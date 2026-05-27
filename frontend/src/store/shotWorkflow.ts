import { create } from "zustand";
import type { Edge, Node } from "@xyflow/react";

import {
  createEdge,
  createNode,
  deleteEdge,
  deleteNode,
  getShotWorkflow,
  patchNode,
  type NodeType,
} from "../api/client";

export type { NodeType };

export type NodeStatus = "idle" | "queued" | "running" | "done" | "error" | "partial";

// Storyboard — see .omc/plans/storyboard-image-node.md §4.1.
// Mirror of the legacy useBoardStore types so NodeCard.tsx (Phase 4
// candidate) doesn't have to change to read from this store.
export type ShotStoryboardStatus =
  | "idle"
  | "queued"
  | "running"
  | "done"
  | "error"
  | "blocked";

export interface StoryboardShot {
  idx: number;
  prompt: string;
  parentShotIdx: number | null;
  mediaId?: string;
  status: ShotStoryboardStatus;
  error?: string;
}

export interface FlowboardNodeData extends Record<string, unknown> {
  type: NodeType;
  shortId: string;
  title: string;
  status?: NodeStatus;
  prompt?: string;
  thumbnailUrl?: string;
  mediaId?: string;
  mediaIds?: (string | null)[];
  slotErrors?: (string | null)[];
  variantCount?: number;
  aspectRatio?: string;
  aiBrief?: string;
  aiBriefStatus?: "pending" | "done" | "failed";
  autoPromptStatus?: "pending" | "done" | "failed";
  renderedAt?: string;
  imageModel?: string;
  videoQuality?: string;
  charCountry?: string;
  charVibe?: string;
  charGender?: string;
  error?: string;
  shots?: StoryboardShot[];
  shotCount?: number;
  narrativeSeed?: string;

  // Phase 4 anime nodes
  scriptText?: string;
  bibleType?: "project" | "scene";
  bibleText?: string;
  masterShotAssetId?: number;
  gateTitle?: string;
  gateNotes?: string;

  // Phase 7 — Seedance 2.0 r2v + audio
  // AudioRefNode: the uploaded voice/audio reference + a human label.
  audioMediaId?: string;
  audioMime?: string;
  voiceDescription?: string;
  // VideoNode multi-ref editor: ordered reference image media_ids (the
  // array order IS the @imageN positional binding) + optional per-ref
  // role hints (UI-only, NOT sent to the API — reserved for Phase 6
  // prompt synthesis to compose semantic @imageN descriptions).
  reference_image_ids?: string[];
  reference_role_hints?: (string | null)[];
  last_frame_asset_id?: string;
  audio_ref_media_id?: string;
  videoModelId?: string;
  duration_seconds?: number;
  resolution?: string;
  generate_audio?: boolean;

  // Phase 8.1 — Manual vs Automation prompt mode (VideoNode only; default
  // "manual"). Manual = paste full prompt, no synth / no Bible.
  prompt_mode?: "manual" | "automation";
  // Per-ref node (Character / VisualAsset): user-assigned @image label
  // (drives positional ref ordering) + optional 2-3 line description.
  reference_label?: string;
  reference_description?: string;
  // Phase 8.1.5 — chosen primary variant (a media_id from mediaIds). Global
  // per node; downstream refs resolve primary_variant_id ?? mediaId ?? mediaIds[0].
  primary_variant_id?: string;
}

export type FlowNode = Node<FlowboardNodeData>;

export interface FlowboardEdgeData extends Record<string, unknown> {
  sourceVariantIdx?: number | null;
}

interface RawEdge {
  id: number;
  source_id: number;
  target_id: number;
  source_variant_idx?: number | null;
}

function edgeFromDto(dto: RawEdge): Edge<FlowboardEdgeData> {
  return {
    id: String(dto.id),
    source: String(dto.source_id),
    target: String(dto.target_id),
    data: { sourceVariantIdx: dto.source_variant_idx ?? null },
  };
}

const TYPE_TITLE: Record<NodeType, string> = {
  character: "Character",
  image: "Image",
  video: "Video",
  prompt: "Prompt",
  note: "Note",
  visual_asset: "Visual asset",
  storyboard: "Storyboard",
  script: "Script",
  bible_ref: "Bible",
  master_shot: "Master shot",
  approval_gate: "Approval gate",
  audio_ref: "Audio ref",
};

const positionTimers = new Map<string, ReturnType<typeof setTimeout>>();

function debouncePosition(rfId: string, fn: () => void, delay = 150) {
  const existing = positionTimers.get(rfId);
  if (existing !== undefined) clearTimeout(existing);
  positionTimers.set(
    rfId,
    setTimeout(() => {
      positionTimers.delete(rfId);
      fn();
    }, delay),
  );
}

interface RawNode {
  id: number;
  short_id: string;
  type: NodeType;
  x: number;
  y: number;
  data: Record<string, unknown>;
  status: NodeStatus;
}

function nodeFromDto(n: RawNode): FlowNode {
  return {
    id: String(n.id),
    type: n.type,
    position: { x: n.x, y: n.y },
    data: {
      type: n.type,
      shortId: n.short_id,
      title: (n.data["title"] as string | undefined) ?? TYPE_TITLE[n.type],
      status: n.status,
      prompt: n.data["prompt"] as string | undefined,
      thumbnailUrl: n.data["thumbnailUrl"] as string | undefined,
      mediaId: n.data["mediaId"] as string | undefined,
      mediaIds: n.data["mediaIds"] as (string | null)[] | undefined,
      slotErrors: n.data["slotErrors"] as (string | null)[] | undefined,
      variantCount: n.data["variantCount"] as number | undefined,
      aspectRatio: n.data["aspectRatio"] as string | undefined,
      aiBrief: n.data["aiBrief"] as string | undefined,
      imageModel: n.data["imageModel"] as string | undefined,
      videoQuality: n.data["videoQuality"] as string | undefined,
      charCountry: n.data["charCountry"] as string | undefined,
      charVibe: n.data["charVibe"] as string | undefined,
      charGender: n.data["charGender"] as string | undefined,
      error: n.data["error"] as string | undefined,
      shots: n.data["shots"] as StoryboardShot[] | undefined,
      shotCount: n.data["shotCount"] as number | undefined,
      narrativeSeed: n.data["narrativeSeed"] as string | undefined,
      scriptText: n.data["scriptText"] as string | undefined,
      bibleType: n.data["bibleType"] as "project" | "scene" | undefined,
      bibleText: n.data["bibleText"] as string | undefined,
      masterShotAssetId: n.data["masterShotAssetId"] as number | undefined,
      gateTitle: n.data["gateTitle"] as string | undefined,
      gateNotes: n.data["gateNotes"] as string | undefined,
    },
  };
}

interface ShotWorkflowState {
  shotId: string | null;
  nodes: FlowNode[];
  edges: Edge<FlowboardEdgeData>[];
  loading: boolean;
  error: string | null;

  loadShotWorkflow(shotId: string): Promise<void>;
  refreshWorkflow(): Promise<void>;
  clearShot(): void;

  addNodeOfType(type: NodeType, position: { x: number; y: number }): Promise<string | null>;
  addReferenceNode(
    ref: {
      mediaId: string;
      aiBrief?: string | null;
      aspectRatio?: string | null;
      kind: string;
      label: string;
    },
    position: { x: number; y: number },
  ): Promise<string | null>;
  persistNodePosition(rfId: string, position: { x: number; y: number }): Promise<void>;
  deleteNodeByRfId(rfId: string): Promise<void>;
  addEdgeFromConnection(source: string, target: string): Promise<void>;
  deleteEdgeByRfId(rfId: string): Promise<void>;
  cloneNodeWithUpstream(rfId: string): Promise<string | null>;

  updateNodeData(rfId: string, partial: Partial<FlowboardNodeData>): void;
  updateEdgeData(edgeId: string, partial: Partial<FlowboardEdgeData>): void;
  setNodes(nodes: FlowNode[]): void;
  setEdges(edges: Edge<FlowboardEdgeData>[]): void;
  clearError(): void;
}

export const useShotWorkflowStore = create<ShotWorkflowState>((set, get) => ({
  shotId: null,
  nodes: [],
  edges: [],
  loading: false,
  error: null,

  async loadShotWorkflow(shotId) {
    // Always switch to the requested shot even if same id — the caller is
    // signaling "refresh / take ownership". Clears prior state immediately
    // so the canvas doesn't briefly render stale nodes from the previous
    // shot mid-fetch (critical for "switch mid-generation" smoke test).
    set({ shotId, nodes: [], edges: [], loading: true, error: null });
    try {
      const wf = await getShotWorkflow(shotId);
      // Guard against late responses after a subsequent switch — if the
      // active shot has changed by the time this returns, drop the result.
      if (get().shotId !== shotId) return;
      set({
        nodes: wf.nodes.map(nodeFromDto),
        edges: wf.edges.map(edgeFromDto),
        loading: false,
      });
    } catch (err) {
      if (get().shotId !== shotId) return;
      set({ loading: false, error: err instanceof Error ? err.message : String(err) });
    }
  },

  async refreshWorkflow() {
    const { shotId } = get();
    if (!shotId) return;
    try {
      const wf = await getShotWorkflow(shotId);
      if (get().shotId !== shotId) return;
      set({
        nodes: wf.nodes.map(nodeFromDto),
        edges: wf.edges.map(edgeFromDto),
      });
    } catch {
      /* polling — next tick retries */
    }
  },

  clearShot() {
    set({ shotId: null, nodes: [], edges: [] });
  },

  async addNodeOfType(type, position) {
    const { shotId } = get();
    if (!shotId) return null;
    const title = TYPE_TITLE[type];
    try {
      const dto = await createNode({
        shot_id: shotId,
        type,
        x: Math.round(position.x),
        y: Math.round(position.y),
        data: { title },
      });
      if (get().shotId !== shotId) return null;
      const node: FlowNode = {
        id: String(dto.id),
        type: dto.type,
        position: { x: dto.x, y: dto.y },
        data: {
          type: dto.type,
          shortId: dto.short_id,
          title: (dto.data["title"] as string | undefined) ?? title,
          status: dto.status,
        },
      };
      set((s) => ({ nodes: [...s.nodes, node] }));
      return node.id;
    } catch {
      // surface silently for now
    }
    return null;
  },

  async addReferenceNode(ref, position) {
    const { shotId } = get();
    if (!shotId) return null;
    const title = ref.label || "Reference";
    try {
      const dto = await createNode({
        shot_id: shotId,
        type: "visual_asset",
        x: Math.round(position.x),
        y: Math.round(position.y),
        data: {
          title,
          mediaId: ref.mediaId,
          aiBrief: ref.aiBrief ?? undefined,
          aspectRatio: ref.aspectRatio ?? undefined,
          status: "done",
          renderedAt: new Date().toISOString(),
        },
      });
      if (get().shotId !== shotId) return null;
      const node: FlowNode = {
        id: String(dto.id),
        type: dto.type,
        position: { x: dto.x, y: dto.y },
        data: {
          type: dto.type,
          shortId: dto.short_id,
          title: (dto.data["title"] as string | undefined) ?? title,
          status: "done",
          mediaId: ref.mediaId,
          aiBrief: ref.aiBrief ?? undefined,
          aspectRatio: ref.aspectRatio ?? undefined,
          renderedAt: new Date().toISOString(),
        },
      };
      set((s) => ({ nodes: [...s.nodes, node] }));
      return node.id;
    } catch {
      // ignore
    }
    return null;
  },

  async persistNodePosition(rfId, position) {
    debouncePosition(rfId, async () => {
      const dbId = parseInt(rfId, 10);
      if (isNaN(dbId)) return;
      try {
        await patchNode(dbId, {
          x: Math.round(position.x),
          y: Math.round(position.y),
        });
      } catch {
        /* ignore */
      }
    });
  },

  async deleteNodeByRfId(rfId) {
    const dbId = parseInt(rfId, 10);
    if (isNaN(dbId)) return;
    const pending = positionTimers.get(rfId);
    if (pending !== undefined) {
      clearTimeout(pending);
      positionTimers.delete(rfId);
    }
    try {
      const { useGenerationStore } = await import("./generation");
      useGenerationStore.getState().cancelGeneration(rfId);
    } catch {
      /* ignore */
    }
    try {
      await deleteNode(dbId);
      set((s) => ({
        nodes: s.nodes.filter((n) => n.id !== rfId),
        edges: s.edges.filter((e) => e.source !== rfId && e.target !== rfId),
      }));
    } catch {
      /* ignore */
    }
  },

  async addEdgeFromConnection(source, target) {
    const { shotId } = get();
    if (!shotId) return;
    const sourceId = parseInt(source, 10);
    const targetId = parseInt(target, 10);
    if (isNaN(sourceId) || isNaN(targetId)) return;
    try {
      const dto = await createEdge({
        shot_id: shotId,
        source_id: sourceId,
        target_id: targetId,
      });
      if (get().shotId !== shotId) return;
      set((s) => ({ edges: [...s.edges, edgeFromDto(dto)] }));
    } catch {
      /* ignore */
    }
  },

  async cloneNodeWithUpstream(rfId) {
    const { shotId, nodes, edges } = get();
    if (!shotId) return null;
    const src = nodes.find((n) => n.id === rfId);
    if (!src) return null;

    const offset = { x: 60, y: 60 };
    const newPos = {
      x: Math.round(src.position.x + offset.x),
      y: Math.round(src.position.y + offset.y),
    };
    const baseTitle = src.data.title ?? TYPE_TITLE[src.data.type];
    const newTitle = baseTitle.endsWith("(variant)")
      ? baseTitle
      : `${baseTitle} (variant)`;

    let nodeDto;
    try {
      nodeDto = await createNode({
        shot_id: shotId,
        type: src.data.type,
        x: newPos.x,
        y: newPos.y,
        data: { title: newTitle },
      });
    } catch {
      return null;
    }
    if (get().shotId !== shotId) return null;

    const newNode: FlowNode = {
      id: String(nodeDto.id),
      type: nodeDto.type,
      position: { x: nodeDto.x, y: nodeDto.y },
      data: {
        type: nodeDto.type,
        shortId: nodeDto.short_id,
        title: (nodeDto.data["title"] as string | undefined) ?? newTitle,
        status: nodeDto.status,
      },
    };
    set((s) => ({ nodes: [...s.nodes, newNode] }));

    const upstreamSourceRfIds = edges
      .filter((e) => e.target === rfId)
      .map((e) => e.source);
    for (const usrc of upstreamSourceRfIds) {
      const sourceId = parseInt(usrc, 10);
      if (isNaN(sourceId)) continue;
      try {
        const eDto = await createEdge({
          shot_id: shotId,
          source_id: sourceId,
          target_id: nodeDto.id,
        });
        if (get().shotId !== shotId) return newNode.id;
        const newEdge: Edge<FlowboardEdgeData> = {
          id: String(eDto.id),
          source: String(eDto.source_id),
          target: String(eDto.target_id),
        };
        set((s) => ({ edges: [...s.edges, newEdge] }));
      } catch {
        /* best-effort */
      }
    }
    return newNode.id;
  },

  async deleteEdgeByRfId(rfId) {
    const dbId = parseInt(rfId, 10);
    if (isNaN(dbId)) return;
    try {
      await deleteEdge(dbId);
      set((s) => ({ edges: s.edges.filter((e) => e.id !== rfId) }));
    } catch {
      /* ignore */
    }
  },

  updateNodeData: (rfId, partial) =>
    set((s) => ({
      nodes: s.nodes.map((n) =>
        n.id === rfId ? { ...n, data: { ...n.data, ...partial } } : n,
      ),
    })),
  updateEdgeData: (edgeId, partial) =>
    set((s) => ({
      edges: s.edges.map((e) =>
        e.id === edgeId
          ? { ...e, data: { ...(e.data ?? {}), ...partial } }
          : e,
      ),
    })),
  setNodes: (nodes) => set({ nodes }),
  setEdges: (edges) => set({ edges }),
  clearError: () => set({ error: null }),
}));
