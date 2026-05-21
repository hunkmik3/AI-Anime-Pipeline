import { useEffect, useRef, useState } from "react";
import type { NodeProps } from "@xyflow/react";

import { patchNode } from "../../api/client";
import { useShotStore } from "../../store/shot";
import {
  useShotWorkflowStore,
  type FlowNode,
  type FlowboardNodeData,
} from "../../store/shotWorkflow";
import { BaseNodeShell } from "./BaseNodeShell";

function ApprovalGateBody({
  rfId,
  data,
}: {
  rfId: string;
  data: FlowboardNodeData;
}) {
  const [title, setTitle] = useState(data.gateTitle ?? "");
  const [notes, setNotes] = useState(data.gateNotes ?? "");

  const currentShot = useShotStore((s) => s.currentShot);
  const isPaused = currentShot?.status === "awaiting_approval";

  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  function persist(next: { gateTitle?: string; gateNotes?: string }) {
    useShotWorkflowStore.getState().updateNodeData(rfId, next);
    if (debounceRef.current !== null) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      const dbId = parseInt(rfId, 10);
      if (!isNaN(dbId)) {
        patchNode(dbId, { data: next }).catch(() => {});
      }
    }, 400);
  }

  useEffect(() => {
    return () => {
      if (debounceRef.current !== null) clearTimeout(debounceRef.current);
    };
  }, []);

  return (
    <div className="node-body node-body--prompt">
      {isPaused && (
        <div
          role="status"
          aria-live="polite"
          style={{
            background: "rgba(245, 179, 1, 0.18)",
            border: "1px solid rgba(245, 179, 1, 0.6)",
            color: "var(--text)",
            padding: "4px 8px",
            borderRadius: 4,
            fontSize: 12,
            marginBottom: 6,
          }}
        >
          ⏸ Awaiting approval
        </div>
      )}
      <input
        type="text"
        className="visual-asset__link-input"
        placeholder="Gate title (e.g. 'Director sign-off')"
        value={title}
        onChange={(e) => {
          setTitle(e.target.value);
          persist({ gateTitle: e.target.value });
        }}
        style={{ width: "100%", marginBottom: 6 }}
      />
      <textarea
        className="prompt-editor"
        placeholder="Notes for the reviewer…"
        value={notes}
        onChange={(e) => {
          setNotes(e.target.value);
          persist({ gateNotes: e.target.value });
        }}
        rows={2}
        style={{ width: "100%" }}
      />
    </div>
  );
}

export function ApprovalGateNode(props: NodeProps<FlowNode>) {
  return (
    <BaseNodeShell
      data={props.data}
      selected={props.selected ?? false}
    >
      <ApprovalGateBody rfId={props.id} data={props.data} />
    </BaseNodeShell>
  );
}
