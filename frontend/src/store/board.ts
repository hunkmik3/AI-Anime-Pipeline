/**
 * Phase 3 transitional proxy.
 *
 * Pre-Phase-3, ``useBoardStore`` owned everything: the board list, the
 * active board id, and the workflow nodes/edges for that board. Phase 3
 * split that responsibility across four stores
 * (``useProjectStore``, ``useSceneStore``, ``useShotStore``,
 * ``useShotWorkflowStore``) but consumers like
 * ``canvas/NodeCard.tsx`` (1651 LOC, Phase 4 candidate) still expect the
 * old API. This file keeps the old surface alive by syncing state from
 * the new substores via ``subscribe`` and forwarding actions through.
 *
 * Phase 4 will delete this file once NodeCard is split.
 */
import { create } from "zustand";
import type { Edge } from "@xyflow/react";

import { useShotWorkflowStore, type FlowNode, type FlowboardNodeData, type FlowboardEdgeData, type NodeType, type NodeStatus } from "./shotWorkflow";
import { useShotStore } from "./shot";
import { useProjectStore } from "./project";

export type { NodeType, NodeStatus, FlowboardNodeData, FlowNode, FlowboardEdgeData };
export type { StoryboardShot, ShotStoryboardStatus as ShotStatus } from "./shotWorkflow";

interface BoardState {
  // ``boardId`` is now the active Shot's UUID (string). Pre-Phase-3 it was
  // a numeric SQLite PK; consumers that did ``parseInt`` on it have been
  // updated to pass UUIDs straight through.
  boardId: string | null;
  boardName: string;
  // Kept for backwards compat. Always empty in the proxy — the new
  // ProjectSidebar reads ``useProjectStore.projects`` directly.
  boards: Array<{ id: string; name: string; created_at: string }>;
  nodes: FlowNode[];
  edges: Edge<FlowboardEdgeData>[];
  loading: boolean;
  error: string | null;

  // Legacy load + switch operations are no-ops here. The new routes
  // (Phase 3 React Router) drive shot loading via ``useShotWorkflowStore.
  // loadShotWorkflow`` directly from the ShotEditor page.
  loadInitialBoard(): Promise<void>;
  refreshBoardState(): Promise<void>;
  refreshBoardList(): Promise<void>;
  renameBoard(name: string): Promise<void>;
  switchBoard(id: string): Promise<void>;
  createNewBoard(name: string): Promise<string | null>;
  deleteBoardById(id: string): Promise<void>;

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

export const useBoardStore = create<BoardState>((set, _get) => {
  const wf = useShotWorkflowStore.getState();
  return {
    boardId: wf.shotId,
    boardName: "",
    boards: [],
    nodes: wf.nodes,
    edges: wf.edges,
    loading: wf.loading,
    error: wf.error,

    async loadInitialBoard() {
      // Phase 3: project bootstrap lives in App.tsx via useProjectStore.
      // The proxy keeps the method around so older imports compile but
      // it's intentionally a no-op.
    },
    async refreshBoardState() {
      await useShotWorkflowStore.getState().refreshWorkflow();
    },
    async refreshBoardList() {
      // Project list maintenance moved to useProjectStore.
    },
    async renameBoard(name) {
      const projId = useProjectStore.getState().currentProjectId;
      if (!projId) return;
      await useProjectStore.getState().renameProject(projId, name);
    },
    async switchBoard(id) {
      await useShotStore.getState().selectShot(id);
      await useShotWorkflowStore.getState().loadShotWorkflow(id);
    },
    async createNewBoard(_name) {
      // Creation goes through the new ProjectListPage / SceneView UI —
      // this legacy entry point is a no-op.
      return null;
    },
    async deleteBoardById(_id) {
      /* superseded by project/scene/shot delete paths */
    },

    addNodeOfType(type, position) {
      return useShotWorkflowStore.getState().addNodeOfType(type, position);
    },
    addReferenceNode(ref, position) {
      return useShotWorkflowStore.getState().addReferenceNode(ref, position);
    },
    persistNodePosition(rfId, position) {
      return useShotWorkflowStore.getState().persistNodePosition(rfId, position);
    },
    deleteNodeByRfId(rfId) {
      return useShotWorkflowStore.getState().deleteNodeByRfId(rfId);
    },
    addEdgeFromConnection(source, target) {
      return useShotWorkflowStore.getState().addEdgeFromConnection(source, target);
    },
    deleteEdgeByRfId(rfId) {
      return useShotWorkflowStore.getState().deleteEdgeByRfId(rfId);
    },
    cloneNodeWithUpstream(rfId) {
      return useShotWorkflowStore.getState().cloneNodeWithUpstream(rfId);
    },

    updateNodeData(rfId, partial) {
      useShotWorkflowStore.getState().updateNodeData(rfId, partial);
    },
    updateEdgeData(edgeId, partial) {
      useShotWorkflowStore.getState().updateEdgeData(edgeId, partial);
    },
    setNodes(nodes) {
      useShotWorkflowStore.getState().setNodes(nodes);
    },
    setEdges(edges) {
      useShotWorkflowStore.getState().setEdges(edges);
    },
    clearError() {
      set({ error: null });
      useShotWorkflowStore.getState().clearError();
    },
  };
});

// ── Cross-store sync ──────────────────────────────────────────────────────
// Mirror the relevant slices from the new stores into ``useBoardStore``
// so existing components that subscribe via ``useBoardStore((s) => s.x)``
// re-render whenever the canonical state changes. Subscribers are set up
// at module load time and never torn down — this proxy lives for one
// phase.

useShotWorkflowStore.subscribe((state) => {
  useBoardStore.setState({
    boardId: state.shotId,
    nodes: state.nodes,
    edges: state.edges,
    loading: state.loading,
    error: state.error,
  });
});

useProjectStore.subscribe((state) => {
  useBoardStore.setState({
    boardName: state.currentProject?.name ?? "",
  });
});
