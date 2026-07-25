import { useState } from "react";
import { NodeResizer, type NodeProps } from "@xyflow/react";

import { patchShotGroup } from "../../api/client";
import { useShotWorkflowStore } from "../../store/shotWorkflow";

const MIN_W = 400;
const MIN_H = 250;

export interface ShotGroupData extends Record<string, unknown> {
  shotId: string;
  label: string;
  sceneLabel: string;
  collapsed: boolean;
  childCount: number;
  /** Absent for non-admins — deleting a shot is admin-only (Phase 9.1). */
  onDelete?: () => void;
  /** Fired after a manual resize persists → parent canvas reflows the stack
   *  so the gap below this group stays constant. */
  onResize?: () => void;
}

/**
 * Phase 8.3 — a shot's bounding-box frame on the SceneCanvas (a React Flow
 * parent/container node; child nodes render inside via parentId). Header
 * carries the label (double-click to rename), a collapse toggle, and a shot
 * badge. Group metadata persists to scene.canvas_state via patchShotGroup.
 */
export function ShotGroupNode({ data, selected }: NodeProps) {
  const d = data as ShotGroupData;
  const [editing, setEditing] = useState(false);
  const [label, setLabel] = useState(d.label);

  function persist(patch: { collapsed?: boolean; label?: string }) {
    useShotWorkflowStore.getState().updateShotGroupLocal(d.shotId, patch);
    void patchShotGroup(d.shotId, patch).catch(() => {});
  }

  return (
    <div className={`shot-group${d.collapsed ? " shot-group--collapsed" : ""}`}>
      {/* Manual resize (Phase 8.3b): drag corners/edges to expand the frame.
          Persists size → groupSize() then respects it over auto-fit. */}
      {!d.collapsed && (
        <NodeResizer
          minWidth={MIN_W}
          minHeight={MIN_H}
          isVisible={selected}
          onResizeEnd={(_e, p) => {
            const size = { w: Math.round(p.width), h: Math.round(p.height) };
            useShotWorkflowStore.getState().updateShotGroupLocal(d.shotId, { size });
            void patchShotGroup(d.shotId, { size }).catch(() => {});
            // Reflow the rest of the stack so groups below shift to keep a
            // constant gap (shrunk → move up; expanded → move down).
            d.onResize?.();
          }}
        />
      )}
      <div className="shot-group__header nodrag">
        <button
          type="button"
          className="shot-group__toggle"
          title={d.collapsed ? "Expand" : "Collapse"}
          onClick={(e) => {
            e.stopPropagation();
            persist({ collapsed: !d.collapsed });
          }}
        >
          {d.collapsed ? "▶" : "▼"}
        </button>
        {editing ? (
          <input
            className="shot-group__label-input"
            style={{ fontSize: 28, fontWeight: 700 }}
            value={label}
            autoFocus
            maxLength={60}
            onChange={(e) => setLabel(e.target.value)}
            onBlur={() => {
              setEditing(false);
              const next = label.trim() || d.label;
              setLabel(next);
              if (next !== d.label) persist({ label: next });
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") (e.target as HTMLInputElement).blur();
            }}
          />
        ) : (
          <span
            className="shot-group__label"
            onDoubleClick={(e) => {
              e.stopPropagation();
              setLabel(d.label);
              setEditing(true);
            }}
            title="Double-click to rename"
          >
            <span className="shot-group__num" style={{ fontSize: 30 }}>
              {d.label}
            </span>
            {d.sceneLabel ? (
              <span className="shot-group__scene" style={{ fontSize: 26 }}>
                {" "}
                — {d.sceneLabel}
              </span>
            ) : null}
            <button
              type="button"
              className="shot-group__rename nodrag"
              title="Rename sequence"
              aria-label="Rename sequence"
              style={{
                marginLeft: 12,
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                gap: 8,
                height: 50,
                flexShrink: 0,
                padding: "0 20px",
                borderRadius: 10,
                background: "rgba(255,255,255,0.06)",
                border: "1px solid var(--border)",
                color: "var(--text)",
                cursor: "pointer",
                fontSize: 16,
                fontWeight: 600,
              }}
              onMouseEnter={(e) => {
                (e.currentTarget as HTMLButtonElement).style.background = "var(--accent)";
                (e.currentTarget as HTMLButtonElement).style.color = "#fff";
              }}
              onMouseLeave={(e) => {
                (e.currentTarget as HTMLButtonElement).style.background = "rgba(255,255,255,0.06)";
                (e.currentTarget as HTMLButtonElement).style.color = "var(--text)";
              }}
              onClick={(e) => {
                e.stopPropagation();
                setLabel(d.label);
                setEditing(true);
              }}
            >
              <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z" />
              </svg>
              Rename
            </button>
          </span>
        )}
        <span className="shot-group__badge">{d.childCount} node{d.childCount === 1 ? "" : "s"}</span>
        {d.onDelete ? (
          <button
            type="button"
            className="shot-group__delete"
            title="Delete this sequence (and its nodes)"
            aria-label="Delete sequence"
            onClick={(e) => {
              e.stopPropagation();
              d.onDelete?.();
            }}
          >
            ✕
          </button>
        ) : null}
      </div>
      {d.collapsed && (
        <div className="shot-group__collapsed-body">collapsed</div>
      )}
    </div>
  );
}
