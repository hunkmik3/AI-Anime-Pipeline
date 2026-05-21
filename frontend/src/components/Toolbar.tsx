import { useState, useRef, type KeyboardEvent } from "react";
import { useProjectStore } from "../store/project";
import { ActivityBell } from "./activity/ActivityBell";
import { AiProviderBadge } from "./AiProviderBadge";
import { SponsorButton } from "./SponsorDialog";

export function Toolbar() {
  const currentProject = useProjectStore((s) => s.currentProject);
  const renameProject = useProjectStore((s) => s.renameProject);
  const boardName = currentProject?.name ?? "";
  const projectId = currentProject?.id ?? null;
  const renameBoard = (name: string) => {
    if (projectId) void renameProject(projectId, name);
  };

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  function startEdit() {
    setDraft(boardName);
    setEditing(true);
    requestAnimationFrame(() => inputRef.current?.select());
  }

  function commitEdit() {
    setEditing(false);
    const trimmed = draft.trim();
    if (trimmed && trimmed !== boardName) {
      renameBoard(trimmed);
    }
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") inputRef.current?.blur();
    if (e.key === "Escape") {
      setEditing(false);
    }
  }

  return (
    <div className="toolbar">
      <span className="toolbar-wordmark">Flowboard</span>
      <span className="toolbar-sep" aria-hidden="true">/</span>
      {editing ? (
        <input
          ref={inputRef}
          className="toolbar-name-input"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commitEdit}
          onKeyDown={onKeyDown}
          aria-label="Board name"
        />
      ) : (
        <button
          className="toolbar-name-btn"
          onClick={startEdit}
          aria-label="Rename board"
          title="Click to rename"
        >
          {boardName || "Untitled"}
        </button>
      )}

      <div className="toolbar-actions">
        <ActivityBell />
        <AiProviderBadge />
        <SponsorButton />
      </div>
    </div>
  );
}
