import { useEffect, useRef, useState } from "react";

import { patchNode } from "../../api/client";
import {
  useShotWorkflowStore,
  type FlowboardNodeData,
} from "../../store/shotWorkflow";

export function EditableTextBody({
  rfId,
  data,
  variant,
}: {
  rfId: string;
  data: FlowboardNodeData;
  variant: "prompt" | "note";
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(data.prompt ?? "");
  const taRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (editing) {
      setDraft(data.prompt ?? "");
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
    if (next !== (data.prompt ?? "")) {
      useShotWorkflowStore.getState().updateNodeData(rfId, { prompt: next });
      const dbId = parseInt(rfId, 10);
      if (!isNaN(dbId)) {
        patchNode(dbId, { data: { prompt: next } }).catch(() => {});
      }
    }
    setEditing(false);
  }

  if (editing) {
    return (
      <div className={`node-body node-body--${variant} node-body--${variant}-edit`}>
        <textarea
          ref={taRef}
          className={`${variant}-editor`}
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
          placeholder={
            variant === "prompt"
              ? "Style direction (e.g. cinematic warm tone, magazine editorial mood). Connect into image/video to feed downstream auto-prompt."
              : "Note, TODO, label…"
          }
        />
      </div>
    );
  }

  const text = data.prompt ?? "";
  const placeholder =
    variant === "prompt"
      ? "Double-click to add direction…"
      : "Double-click to add note…";

  return (
    <div
      className={`node-body node-body--${variant}`}
      onDoubleClick={() => setEditing(true)}
      title="Double-click to edit"
    >
      {variant === "prompt" ? (
        <pre className="prompt-text">{text || placeholder}</pre>
      ) : (
        <p className="note-text">{text || placeholder}</p>
      )}
    </div>
  );
}
