import type { NodeType } from "../store/shotWorkflow";

/**
 * The single source of truth for what the "Add node" palette offers.
 *
 * Two canvases render this list (AddNodePalette on the single-shot canvas,
 * SceneCanvasToolbar on the multi-shot one). It used to be copy-pasted into
 * both, so trimming one left the other still offering the retired nodes —
 * hence this shared module.
 *
 * Trimmed to what the studio actually uses: one image ref, one audio ref, one
 * video ref, and the two generators. Logic (script/prompt/approval), Misc
 * (note/storyboard) and the Master/Bible refs are no longer offered.
 *
 * "Image ref" maps to the `character` type on purpose — it's the superset of
 * the old Character/Visual pair: its dialog can either build a portrait from
 * the presets or send a prompt verbatim ("custom" mode), which is all a plain
 * visual reference needs. The retired types still render, so existing nodes
 * keep working; they just can't be created from here anymore.
 */
export interface Chip {
  type: NodeType;
  icon: string;
  label: string;
}

export interface Group {
  label: string;
  chips: Chip[];
}

// Icons kept inline (existing convention — no icon-font dependency).
export const NODE_GROUPS: Group[] = [
  {
    label: "Refs",
    chips: [
      { type: "character", icon: "◎", label: "Image ref" },
      { type: "audio_ref", icon: "🔊", label: "Audio" },
      { type: "video_ref", icon: "🎬", label: "Video ref" },
    ],
  },
  {
    label: "Generation",
    chips: [
      { type: "image", icon: "▣", label: "Image" },
      { type: "video", icon: "▶", label: "Video" },
      { type: "seed_audio", icon: "🎵", label: "Audio Gen" },
    ],
  },
];
