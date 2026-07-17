import { useState } from "react";

import type { NodeType } from "../store/shotWorkflow";
import { NODE_GROUPS as GROUPS, type Chip } from "./nodePalette";

// Flattened list for the right-click context menu (same types as the palette).
export const SCENE_NODE_TYPES: Chip[] = GROUPS.flatMap((g) => g.chips);

export function SceneCanvasToolbar({ onAdd }: { onAdd: (type: NodeType) => void }) {
  const [collapsed, setCollapsed] = useState(false);

  return (
    <div
      className={`add-node-palette${collapsed ? " add-node-palette--collapsed" : ""}`}
      aria-label="Add node"
    >
      <div className="add-node-palette__header">
        <span className="add-node-palette__title">Add node</span>
        <button
          type="button"
          className="add-node-palette__toggle"
          aria-label={collapsed ? "Expand palette" : "Collapse palette"}
          aria-expanded={!collapsed}
          onClick={() => setCollapsed((c) => !c)}
          title={collapsed ? "Expand" : "Collapse"}
        >
          {collapsed ? "◀" : "▶"}
        </button>
      </div>

      {!collapsed && (
        <div className="add-node-palette__sections">
          {GROUPS.map((group) => (
            <div key={group.label} className="add-node-group" role="group" aria-label={group.label}>
              <span className="add-node-group-label">{group.label}</span>
              <div className="add-node-group__grid">
                {group.chips.map((chip) => (
                  <button
                    key={chip.type}
                    type="button"
                    className="add-node-chip"
                    aria-label={`Add ${chip.label} node`}
                    onClick={() => onAdd(chip.type)}
                  >
                    <span className="add-node-chip__icon" aria-hidden="true">{chip.icon}</span>
                    <span className="add-node-chip__label">{chip.label}</span>
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
