import { useState } from "react";
import { useReactFlow } from "@xyflow/react";

import { useShotWorkflowStore, type NodeType } from "../store/shotWorkflow";
import { NODE_GROUPS as GROUPS } from "./nodePalette";

export function AddNodePalette() {
  const { screenToFlowPosition } = useReactFlow();
  const addNodeOfType = useShotWorkflowStore((s) => s.addNodeOfType);
  const [collapsed, setCollapsed] = useState(false);

  function handleAdd(type: NodeType) {
    const position = screenToFlowPosition({
      x: window.innerWidth / 2,
      y: window.innerHeight / 2,
    });
    addNodeOfType(type, position);
  }

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
            <div
              key={group.label}
              className="add-node-group"
              role="group"
              aria-label={group.label}
            >
              <span className="add-node-group-label">{group.label}</span>
              <div className="add-node-group__grid">
                {group.chips.map((chip) => (
                  <button
                    key={chip.type}
                    type="button"
                    className="add-node-chip"
                    aria-label={`Add ${chip.label} node`}
                    onClick={() => handleAdd(chip.type)}
                  >
                    <span className="add-node-chip__icon" aria-hidden="true">
                      {chip.icon}
                    </span>
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
