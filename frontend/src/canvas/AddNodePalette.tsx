import { useReactFlow } from "@xyflow/react";

import { useShotWorkflowStore, type NodeType } from "../store/shotWorkflow";

interface Chip {
  type: NodeType;
  icon: string;
  label: string;
}

interface Group {
  label: string;
  chips: Chip[];
}

const GROUPS: Group[] = [
  {
    label: "Refs",
    chips: [
      { type: "character", icon: "◎", label: "Character" },
      { type: "visual_asset", icon: "◇", label: "Visual asset" },
      { type: "master_shot", icon: "★", label: "Master shot" },
      { type: "bible_ref", icon: "📖", label: "Bible" },
    ],
  },
  {
    label: "Generation",
    chips: [
      { type: "image", icon: "▣", label: "Image" },
      { type: "video", icon: "▶", label: "Video" },
    ],
  },
  {
    label: "Logic",
    chips: [
      { type: "script", icon: "📝", label: "Script" },
      { type: "prompt", icon: "✦", label: "Prompt" },
      { type: "approval_gate", icon: "⏸", label: "Approval gate" },
    ],
  },
  {
    label: "Misc",
    chips: [
      { type: "note", icon: "✎", label: "Note" },
      { type: "storyboard", icon: "▦", label: "Storyboard" },
    ],
  },
];

export function AddNodePalette() {
  const { screenToFlowPosition } = useReactFlow();
  const addNodeOfType = useShotWorkflowStore((s) => s.addNodeOfType);

  function handleAdd(type: NodeType) {
    const position = screenToFlowPosition({
      x: window.innerWidth / 2,
      y: window.innerHeight / 2,
    });
    addNodeOfType(type, position);
  }

  return (
    <div className="add-node-palette" aria-label="Add node">
      <span className="add-node-plus" aria-hidden="true">+</span>
      {GROUPS.map((group) => (
        <div key={group.label} className="add-node-group" role="group" aria-label={group.label}>
          <span className="add-node-group-label">{group.label}</span>
          {group.chips.map((chip) => (
            <button
              key={chip.type}
              className="add-node-chip"
              aria-label={`Add ${chip.label} node`}
              onClick={() => handleAdd(chip.type)}
            >
              <span aria-hidden="true">{chip.icon}</span>
              {chip.label}
            </button>
          ))}
        </div>
      ))}
    </div>
  );
}
