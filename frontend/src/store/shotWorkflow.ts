import { create } from "zustand";
import type { Edge, Node } from "@xyflow/react";

import {
  createEdge,
  createNode,
  deleteEdge,
  deleteNode,
  getSceneCanvas,
  getShotWorkflow,
  patchNode,
  type NodeType,
  type ShotGroup,
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
  // SeedAudioNode (BytePlus Seed Audio 1.0): text → full audio scene. The
  // generated clip lands in audioMediaId (so it plays/downloads and can feed a
  // VideoNode's @audio like an AudioRefNode). These are its input settings.
  seedPrompt?: string;
  seedFormat?: string;          // wav | mp3 | pcm | ogg_opus
  seedSampleRate?: number;
  seedSpeechRate?: number;      // -50..100 (0 = normal)
  seedLoudnessRate?: number;
  seedPitchRate?: number;       // -12..12
  seedAudioRefs?: string[];     // ≤3 URLs / media_ids → @audio1..3 (voice clone)
  seedImageRef?: string;        // 1 image URL / media_id (mutually exclusive)
  seedDuration?: number;        // output duration of the last generation (s)
  seedWidth?: number;           // user-resized prompt-box size (card grows to fit)
  seedHeight?: number;
  // VideoRefNode: an uploaded reference video (Seedance 2.0 r2v video ref).
  // Fed downstream to a connected VideoNode → reference_videos; the worker
  // hoists it to a public R2 URL on submit (Avis has no inline video upload).
  videoRefMediaId?: string;
  videoRefMime?: string;
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
  kycMode?: boolean;
  // DanceSee B2B unmoderated path (/api/v1/b2b/*). Seedance 2.0/2.5 only.
  contentFilterDisabled?: boolean;

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
  // Phase 8.1.5d — client-side gen progress (video). genProgress 0-100,
  // genPhase = queued | generating (estimate from elapsed/duration; the API
  // returns no real %). Cleared on done/error.
  genProgress?: number;
  genPhase?: "queued" | "generating";
  // Phase 8.3 — owning shot id (set when loaded via the multi-shot
  // SceneCanvas so nodes can be grouped by shot). Absent in single-shot mode.
  shotId?: string;

  // Phase 8.4 — frame extraction + cross-shot continuity.
  // On the extracted-frame visual_asset (lives in the source shot):
  source_type?: "extracted_frame";
  source_time?: number; // seconds into the source video
  source_video_node?: string; // rfId of the Video node it was cut from (virtual source edge)
  // On the continuity clone (an `image` node in the target shot → i2v first_frame):
  continuity_source_media?: string; // the frame media_id
  continuity_from_node?: string; // rfId of the extracted-frame node (virtual continuity edge)
  continuity_from_shot?: string; // source shot id (for the prompt hint + label)
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
  video_ref: "Video ref",
  seed_audio: "Audio Gen",
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
  const fn: FlowNode = {
    id: String(n.id),
    type: n.type,
    position: { x: n.x, y: n.y },
    data: {
      // Round-trip ALL persisted data fields first (duration_seconds,
      // resolution, generate_audio, videoModelId, reference_label/description,
      // kycMode, contentFilterDisabled, videoRefMediaId, audioMediaId, …). Without this, any field not
      // explicitly re-listed below silently reverts to its default on reload.
      ...(n.data as Partial<FlowboardNodeData>),
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
      // Phase 8.4 — frame extraction + continuity metadata (must round-trip so
      // the virtual source/continuity edges survive a reload).
      source_type: n.data["source_type"] as "extracted_frame" | undefined,
      source_time: n.data["source_time"] as number | undefined,
      source_video_node: n.data["source_video_node"] as string | undefined,
      continuity_source_media: n.data["continuity_source_media"] as string | undefined,
      continuity_from_node: n.data["continuity_from_node"] as string | undefined,
      continuity_from_shot: n.data["continuity_from_shot"] as string | undefined,
    },
  };
  return fn;
}

interface ShotWorkflowState {
  shotId: string | null;
  // Phase 8.3: when the multi-shot SceneCanvas is active, sceneId is set and
  // nodes/edges hold the WHOLE scene's graph (each node's data.shotId tags
  // its owning shot). shotId stays null in scene mode.
  sceneId: string | null;
  shotGroups: ShotGroup[];
  nodes: FlowNode[];
  edges: Edge<FlowboardEdgeData>[];
  loading: boolean;
  error: string | null;

  loadShotWorkflow(shotId: string): Promise<void>;
  loadSceneCanvas(sceneId: string): Promise<void>;
  setShotGroups(groups: ShotGroup[]): void;
  updateShotGroupLocal(shotId: string, patch: Partial<ShotGroup>): void;
  refreshWorkflow(): Promise<void>;
  clearShot(): void;

  addNodeOfType(type: NodeType, position: { x: number; y: number }): Promise<string | null>;
  // Phase 8.3 — add a node into a SPECIFIC shot (multi-shot SceneCanvas).
  addNodeToShot(
    shotId: string,
    type: NodeType,
    position: { x: number; y: number },
  ): Promise<string | null>;
  // Duplicate one node (same shot, offset position, cloned data). Returns the
  // new node id.
  duplicateNode(id: string): Promise<string | null>;
  // Create a node of `type` in `shotId` with explicit `data` (offset near the
  // source node). Used to "reuse" a past generation from a node's history.
  addNodeToShotWithData(
    sourceRfId: string,
    type: NodeType,
    data: Record<string, unknown>,
  ): Promise<string | null>;
  // Clone every node + intra-shot edge of `srcShotId` into `destShotId` (used to
  // duplicate a whole sequence; caller reloads the canvas to render the frame).
  cloneShotContents(srcShotId: string, destShotId: string): Promise<void>;
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
  /** Drop an uploaded file onto the canvas → spawn the matching node:
   *  image → visual_asset, audio → audio_ref, video → video_ref. The media is
   *  already uploaded (caller has the media_id); this only records the node. */
  addUploadedMediaNode(
    kind: "image" | "audio" | "video",
    mediaId: string,
    position: { x: number; y: number },
    title?: string,
    // Explicit target shot — required on the episode canvas (SceneCanvas), where
    // there is no single "current" shot; omit on the single-sequence canvas.
    shotId?: string,
  ): Promise<string | null>;
  persistNodePosition(rfId: string, position: { x: number; y: number }): Promise<void>;
  deleteNodeByRfId(rfId: string): Promise<void>;
  addEdgeFromConnection(source: string, target: string): Promise<void>;
  // Phase 8.3 — connect nodes on the multi-shot SceneCanvas. The edge is
  // owned by the source node's shot (Edge.shot_id). Works in scene mode
  // where the single `shotId` is null.
  addSceneEdge(source: string, target: string): Promise<void>;
  deleteEdgeByRfId(rfId: string): Promise<void>;
  cloneNodeWithUpstream(rfId: string): Promise<string | null>;
  // Phase 8.4 — create a visual_asset node from an extracted frame, placed
  // beside its source Video node in the same shot.
  addExtractedFrameNode(
    videoRfId: string,
    frameMediaId: string,
    time: number,
  ): Promise<string | null>;
  // Phase 8.4 — clone an extracted frame into another shot as an `image` node
  // (= i2v first_frame slot) carrying continuity metadata; auto-wire it to the
  // target shot's first Video node when one exists. Returns the new node id.
  sendFrameAsContinuity(
    frameNodeId: string,
    targetShotId: string,
  ): Promise<string | null>;

  updateNodeData(rfId: string, partial: Partial<FlowboardNodeData>): void;
  updateEdgeData(edgeId: string, partial: Partial<FlowboardEdgeData>): void;
  setNodes(nodes: FlowNode[]): void;
  setEdges(edges: Edge<FlowboardEdgeData>[]): void;
  clearError(): void;
}

export const useShotWorkflowStore = create<ShotWorkflowState>((set, get) => ({
  shotId: null,
  sceneId: null,
  shotGroups: [],
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

  async loadSceneCanvas(sceneId) {
    // Multi-shot mode: load the whole scene's graph (nodes across all shots)
    // + the shot_groups layout. shotId stays null so single-shot helpers
    // (addNodeOfType etc.) no-op; SceneCanvas drives group/position writes.
    set({ sceneId, shotId: null, nodes: [], edges: [], shotGroups: [], loading: true, error: null });
    try {
      const canvas = await getSceneCanvas(sceneId);
      if (get().sceneId !== sceneId) return;
      const nodes: FlowNode[] = canvas.nodes.map((n) => {
        const fn = nodeFromDto({
          id: n.id,
          short_id: n.short_id,
          type: n.type as NodeType,
          x: n.x,
          y: n.y,
          data: n.data,
          status: n.status as NodeStatus,
        });
        fn.data.shotId = n.shot_id; // tag owning shot for grouping
        return fn;
      });
      const edges = canvas.edges.map((e) =>
        edgeFromDto({
          id: e.id,
          source_id: e.source_id,
          target_id: e.target_id,
          source_variant_idx: e.source_variant_idx,
        }),
      );
      set({ nodes, edges, shotGroups: canvas.shot_groups, loading: false });
    } catch (err) {
      if (get().sceneId !== sceneId) return;
      set({ loading: false, error: err instanceof Error ? err.message : String(err) });
    }
  },

  setShotGroups(groups) {
    set({ shotGroups: groups });
  },

  updateShotGroupLocal(shotId, patch) {
    set((s) => ({
      shotGroups: s.shotGroups.map((g) =>
        g.shot_id === shotId ? { ...g, ...patch } : g,
      ),
    }));
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
    set({ shotId: null, sceneId: null, shotGroups: [], nodes: [], edges: [] });
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

  async addNodeToShot(shotId, type, position) {
    const title = TYPE_TITLE[type];
    try {
      const dto = await createNode({
        shot_id: shotId,
        type,
        x: Math.round(position.x),
        y: Math.round(position.y),
        data: { title },
      });
      const node = nodeFromDto({
        id: dto.id,
        short_id: dto.short_id,
        type: dto.type,
        x: dto.x,
        y: dto.y,
        data: dto.data,
        status: dto.status,
      });
      node.data.shotId = shotId; // tag for SceneCanvas grouping
      set((s) => ({ nodes: [...s.nodes, node] }));
      return node.id;
    } catch {
      return null;
    }
  },

  async duplicateNode(id) {
    const src = get().nodes.find((n) => n.id === id);
    if (!src) return null;
    const shotId = (src.data.shotId as string | undefined) ?? get().shotId ?? undefined;
    if (!shotId) return null;
    // Clone all persisted data (mediaId, settings, prompt, audioMediaId, …);
    // drop shortId (the server mints a fresh one).
    const cloneData: Record<string, unknown> = { ...src.data };
    delete cloneData.shortId;
    try {
      const dto = await createNode({
        shot_id: shotId,
        type: src.data.type,
        x: Math.round(src.position.x + 48),
        y: Math.round(src.position.y + 48),
        data: cloneData,
      });
      const node = nodeFromDto({
        id: dto.id, short_id: dto.short_id, type: dto.type,
        x: dto.x, y: dto.y, data: dto.data, status: dto.status,
      });
      node.data.shotId = shotId;
      // createNode always starts "idle"; carry the source's status so a copied
      // finished node still shows its media (persist so a reload keeps it).
      const st = src.data.status;
      if (st && st !== "idle") {
        node.data.status = st;
        patchNode(dto.id, { status: st }).catch(() => {});
      }
      set((s) => ({ nodes: [...s.nodes, node] }));
      return node.id;
    } catch {
      return null;
    }
  },

  async addNodeToShotWithData(sourceRfId, type, data) {
    const src = get().nodes.find((n) => n.id === sourceRfId);
    const shotId = (src?.data.shotId as string | undefined) ?? get().shotId ?? undefined;
    if (!shotId) return null;
    // Offset from the source so the new node doesn't land exactly on top of it.
    const x = Math.round((src?.position.x ?? 80) + 60);
    const y = Math.round((src?.position.y ?? 80) + 60);
    try {
      const dto = await createNode({ shot_id: shotId, type, x, y, data });
      const node = nodeFromDto({
        id: dto.id, short_id: dto.short_id, type: dto.type,
        x: dto.x, y: dto.y, data: dto.data, status: dto.status,
      });
      node.data.shotId = shotId; // tag for SceneCanvas grouping
      set((s) => ({ nodes: [...s.nodes, node] }));
      return node.id;
    } catch {
      return null;
    }
  },

  async cloneShotContents(srcShotId, destShotId) {
    const srcNodes = get().nodes.filter((n) => n.data.shotId === srcShotId);
    const idMap = new Map<string, string>(); // old node id → new node id
    for (const src of srcNodes) {
      const cloneData: Record<string, unknown> = { ...src.data };
      delete cloneData.shortId;
      try {
        const dto = await createNode({
          shot_id: destShotId,
          type: src.data.type,
          x: Math.round(src.position.x),
          y: Math.round(src.position.y),
          data: cloneData,
        });
        idMap.set(src.id, String(dto.id));
        const st = src.data.status;
        if (st && st !== "idle") patchNode(dto.id, { status: st }).catch(() => {});
      } catch {
        /* skip a node that fails to clone */
      }
    }
    // Re-create edges whose BOTH ends were cloned (intra-shot wiring).
    for (const e of get().edges) {
      const ns = idMap.get(e.source);
      const nt = idMap.get(e.target);
      if (!ns || !nt) continue;
      try {
        await createEdge({
          shot_id: destShotId,
          source_id: parseInt(ns, 10),
          target_id: parseInt(nt, 10),
        });
      } catch {
        /* skip */
      }
    }
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

  async addUploadedMediaNode(kind, mediaId, position, title, shotId) {
    // Explicit shot (episode canvas) or the current single-sequence canvas.
    const explicit = shotId != null;
    const targetShot = shotId ?? get().shotId;
    if (!targetShot) return null;
    const type: NodeType =
      kind === "audio" ? "audio_ref" : kind === "video" ? "video_ref" : "visual_asset";
    const label = title || (kind === "audio" ? "Audio" : kind === "video" ? "Video" : "Reference");
    // Each node type reads its media off a different key (see collectUpstream*
    // in the generation store and the *Node components).
    const data: Record<string, unknown> =
      kind === "image"
        ? { type, title: label, mediaId, status: "done", renderedAt: new Date().toISOString() }
        : kind === "audio"
          ? { type, title: label, audioMediaId: mediaId, status: "done" }
          : { type, title: label, videoRefMediaId: mediaId, status: "done" };
    try {
      const dto = await createNode({
        shot_id: targetShot,
        type,
        x: Math.round(position.x),
        y: Math.round(position.y),
        data,
      });
      // Guard against a stale single-sequence canvas only when using the current
      // shot; an explicit target (episode canvas) has no "current shot" to check.
      if (!explicit && get().shotId !== targetShot) return null;
      const node: FlowNode = {
        id: String(dto.id),
        type: dto.type,
        position: { x: dto.x, y: dto.y },
        // shotId tags the node for SceneCanvas group parenting (see groupChildren).
        data: { ...data, type: dto.type, shortId: dto.short_id, shotId: targetShot } as FlowboardNodeData,
      };
      set((s) => ({ nodes: [...s.nodes, node] }));
      // createNode always starts "idle"; persist "done" so a reload keeps the
      // media showing rather than an empty placeholder.
      patchNode(dto.id, { status: "done" }).catch(() => {});
      return node.id;
    } catch {
      // surfaced by the caller
    }
    return null;
  },

  async addExtractedFrameNode(videoRfId, frameMediaId, time) {
    const video = get().nodes.find((n) => n.id === videoRfId);
    const shotId = video?.data.shotId;
    if (!video || !shotId) return null;
    const label = `Frame @${time.toFixed(1)}s`;
    try {
      const dto = await createNode({
        shot_id: shotId,
        type: "visual_asset",
        x: Math.round(video.position.x + 320),
        y: Math.round(video.position.y),
        data: {
          title: label,
          mediaId: frameMediaId,
          source_type: "extracted_frame",
          source_time: time,
          source_video_node: videoRfId,
          renderedAt: new Date().toISOString(),
        },
      });
      // createNode can't set status; persist done so reloads keep it (the
      // thumbnail renders off mediaId regardless, but keep state honest).
      patchNode(dto.id, { status: "done" }).catch(() => {});
      const node = nodeFromDto({
        id: dto.id,
        short_id: dto.short_id,
        type: dto.type,
        x: dto.x,
        y: dto.y,
        data: dto.data,
        status: "done",
      });
      node.data.shotId = shotId;
      set((s) => ({ nodes: [...s.nodes, node] }));
      return node.id;
    } catch {
      return null;
    }
  },

  async sendFrameAsContinuity(frameNodeId, targetShotId) {
    const frame = get().nodes.find((n) => n.id === frameNodeId);
    const frameMediaId = frame?.data.mediaId;
    if (!frame || !frameMediaId) return null;
    const sourceShotId = frame.data.shotId;
    const time = typeof frame.data.source_time === "number" ? frame.data.source_time : 0;

    // Drop the clone below any existing nodes in the target shot so it doesn't
    // land on top of them (positions are shot-local under extent:"parent").
    const targetNodes = get().nodes.filter((n) => n.data.shotId === targetShotId);
    const maxBottom = targetNodes.reduce((m, n) => Math.max(m, n.position.y + 240), 100);
    const pos = { x: 140, y: Math.round(maxBottom + 40) };

    let node: FlowNode;
    try {
      const dto = await createNode({
        shot_id: targetShotId,
        // `image` (not visual_asset) so an edge into the shot's Video node is
        // picked up as the i2v first_frame (strongest Seedance continuity),
        // not an r2v reference_image.
        type: "image",
        x: pos.x,
        y: pos.y,
        data: {
          title: `Continuity @${time.toFixed(1)}s`,
          mediaId: frameMediaId,
          continuity_source_media: frameMediaId,
          continuity_from_node: frameNodeId,
          continuity_from_shot: sourceShotId,
          source_time: time,
          renderedAt: new Date().toISOString(),
        },
      });
      patchNode(dto.id, { status: "done" }).catch(() => {});
      node = nodeFromDto({
        id: dto.id,
        short_id: dto.short_id,
        type: dto.type,
        x: dto.x,
        y: dto.y,
        data: dto.data,
        status: "done",
      });
      node.data.shotId = targetShotId;
      set((s) => ({ nodes: [...s.nodes, node] }));
    } catch {
      return null;
    }

    // Auto-wire to the target shot's first Video node → i2v first_frame.
    const targetVideo = get().nodes.find(
      (n) => n.data.shotId === targetShotId && n.data.type === "video",
    );
    if (targetVideo) {
      const sourceId = parseInt(node.id, 10);
      const targetId = parseInt(targetVideo.id, 10);
      if (!isNaN(sourceId) && !isNaN(targetId)) {
        try {
          const edgeDto = await createEdge({
            shot_id: targetShotId,
            source_id: sourceId,
            target_id: targetId,
          });
          set((s) => ({ edges: [...s.edges, edgeFromDto(edgeDto)] }));
        } catch {
          /* leave unwired — user can connect manually */
        }
      }
    }
    return node.id;
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

  async addSceneEdge(source, target) {
    const nodes = get().nodes;
    const src = nodes.find((n) => n.id === source);
    const tgt = nodes.find((n) => n.id === target);
    // The edge lives in the source node's shot (fall back to target's).
    const shotId = src?.data.shotId ?? tgt?.data.shotId;
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
