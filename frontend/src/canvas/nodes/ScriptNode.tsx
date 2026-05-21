import { useEffect, useRef, useState } from "react";
import type { NodeProps } from "@xyflow/react";

import { patchNode } from "../../api/client";
import {
  useShotWorkflowStore,
  type FlowNode,
  type FlowboardNodeData,
} from "../../store/shotWorkflow";
import { BaseNodeShell } from "./BaseNodeShell";

function ScriptBody({ rfId, data }: { rfId: string; data: FlowboardNodeData }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(data.scriptText ?? "");
  const taRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (editing) {
      setDraft(data.scriptText ?? "");
      requestAnimationFrame(() => {
        const ta = taRef.current;
        if (ta) {
          ta.focus();
          ta.setSelectionRange(ta.value.length, ta.value.length);
        }
      });
    }
  }, [editing]);

  function save() {
    const next = draft;
    if (next !== (data.scriptText ?? "")) {
      useShotWorkflowStore.getState().updateNodeData(rfId, { scriptText: next });
      const dbId = parseInt(rfId, 10);
      if (!isNaN(dbId)) {
        patchNode(dbId, { data: { scriptText: next } }).catch(() => {});
      }
    }
    setEditing(false);
  }

  if (editing) {
    return (
      <div className="node-body node-body--prompt node-body--prompt-edit">
        <textarea
          ref={taRef}
          className="prompt-editor"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={save}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.preventDefault();
              setEditing(false);
            } else if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
              e.preventDefault();
              save();
            }
          }}
          placeholder="Paste shot script in Vietnamese (or any language). Connects downstream to feed image/video prompt synthesis."
        />
      </div>
    );
  }

  const text = data.scriptText ?? "";
  return (
    <div
      className="node-body node-body--prompt"
      onDoubleClick={() => setEditing(true)}
      title="Double-click to edit"
    >
      <pre className="prompt-text">{text || "Double-click to add script…"}</pre>
    </div>
  );
}

export function ScriptNode(props: NodeProps<FlowNode>) {
  return (
    <BaseNodeShell
      data={props.data}
      selected={props.selected ?? false}
      showTargetHandle={false}
    >
      <ScriptBody rfId={props.id} data={props.data} />
    </BaseNodeShell>
  );
}
