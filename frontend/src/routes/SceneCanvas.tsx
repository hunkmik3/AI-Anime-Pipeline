import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  applyNodeChanges,
  useReactFlow,
  type Connection,
  type Node,
  type NodeChange,
  type OnNodeDrag,
} from "@xyflow/react";

import { autoMigrateScene, deleteShot as deleteShotApi, patchShotGroup } from "../api/client";
import { nodeTypes } from "../canvas/nodes";
import { SceneCanvasToolbar, SCENE_NODE_TYPES } from "../canvas/SceneCanvasToolbar";
import { VariantEdge } from "../canvas/VariantEdge";
import { useGenerationStore } from "../store/generation";
import { useProjectStore } from "../store/project";
import { useSceneStore } from "../store/scene";
import { useShotStore } from "../store/shot";
import { useShotWorkflowStore, type FlowNode, type NodeType } from "../store/shotWorkflow";

const edgeTypes = { default: VariantEdge };

// Group-frame sizing (auto-fit to child bbox; min sizes keep empty shots usable).
const NODE_W = 260;
const NODE_H = 240;
const PAD = 48;
const MIN_W = 600; // new/empty shots get a usable ~3-4-node-wide frame
const MIN_H = 300;
const COLLAPSED_W = 280;
const COLLAPSED_H = 120;

const SHOT_DEFAULT_X = 100; // alignment x for the vertical shot stack
const SHOT_VERTICAL_GAP = 100; // gap below the previous shot's bottom edge

const GROUP_PREFIX = "group-";

type ShotGroup = ReturnType<typeof useShotWorkflowStore.getState>["shotGroups"][number];

/** Group frame size: a manual size (user-resized) wins; otherwise auto-fit
 * to the child nodes' bbox. Both min-clamped. */
function groupSize(g: ShotGroup, children: FlowNode[]): { w: number; h: number } {
  if (g.collapsed) return { w: COLLAPSED_W, h: COLLAPSED_H };
  if (g.size) {
    return { w: Math.max(MIN_W, g.size.w), h: Math.max(MIN_H, g.size.h) };
  }
  let w = MIN_W;
  let h = MIN_H;
  for (const c of children) {
    w = Math.max(w, c.position.x + NODE_W + PAD);
    h = Math.max(h, c.position.y + NODE_H + PAD);
  }
  return { w, h };
}

function groupChildren(storeNodes: FlowNode[]): Map<string, FlowNode[]> {
  const byShot = new Map<string, FlowNode[]>();
  for (const n of storeNodes) {
    const sid = n.data.shotId;
    if (!sid) continue;
    (byShot.get(sid) ?? byShot.set(sid, []).get(sid)!).push(n);
  }
  return byShot;
}

/** Build the React Flow node array: one shotGroup container per group
 * (auto-sized to its children) followed by that group's child nodes
 * (parentId set → RF moves them with the group, contains them). */
function buildRfNodes(
  storeNodes: FlowNode[],
  groups: ShotGroup[],
  sceneLabel: string,
  onDeleteShot: (shotId: string) => void,
  onResize: () => void,
): FlowNode[] {
  const byShot = groupChildren(storeNodes);
  const out: FlowNode[] = [];
  const ordered = [...groups].sort((a, b) => a.order - b.order);
  for (const g of ordered) {
    const children = byShot.get(g.shot_id) ?? [];
    const { w, h } = groupSize(g, children);
    out.push({
      id: `${GROUP_PREFIX}${g.shot_id}`,
      type: "shotGroup",
      position: g.position,
      draggable: true,
      selectable: true,
      deletable: false, // shot frames are deleted via the ✕ (persisted), not the Delete key

      data: {
        type: "shotGroup",
        shortId: g.shot_id,
        title: g.label,
        shotId: g.shot_id,
        label: g.label,
        sceneLabel,
        collapsed: g.collapsed,
        childCount: children.length,
        onDelete: () => onDeleteShot(g.shot_id),
        onResize,
      } as unknown as FlowNode["data"],
      style: { width: w, height: h },
    } as FlowNode);
    if (!g.collapsed) {
      for (const c of children) {
        out.push({ ...c, parentId: `${GROUP_PREFIX}${g.shot_id}`, extent: "parent" });
      }
    }
  }
  return out;
}

/** Where to drop a new shot's frame: aligned x, just below the lowest
 * existing group's bottom edge + a small gap (vertical stack). */
function nextShotPosition(groups: ShotGroup[], storeNodes: FlowNode[]): { x: number; y: number } {
  if (groups.length === 0) return { x: SHOT_DEFAULT_X, y: 100 };
  const byShot = groupChildren(storeNodes);
  let maxBottom = -Infinity;
  for (const g of groups) {
    const { h } = groupSize(g, byShot.get(g.shot_id) ?? []);
    maxBottom = Math.max(maxBottom, g.position.y + h);
  }
  const x = groups[0]?.position.x ?? SHOT_DEFAULT_X;
  return { x, y: maxBottom + SHOT_VERTICAL_GAP };
}

function SceneCanvasInner({ projectId, sceneId }: { projectId: string; sceneId: string }) {
  const currentProject = useProjectStore((s) => s.currentProject);
  const currentProjectId = useProjectStore((s) => s.currentProjectId);
  const selectProject = useProjectStore((s) => s.selectProject);
  const currentScene = useSceneStore((s) => s.currentScene);
  const selectScene = useSceneStore((s) => s.selectScene);
  const createShot = useShotStore((s) => s.createShot);

  const storeNodes = useShotWorkflowStore((s) => s.nodes);
  const edges = useShotWorkflowStore((s) => s.edges);
  const shotGroups = useShotWorkflowStore((s) => s.shotGroups);
  const loading = useShotWorkflowStore((s) => s.loading);
  const loadSceneCanvas = useShotWorkflowStore((s) => s.loadSceneCanvas);
  const setNodesInStore = useShotWorkflowStore((s) => s.setNodes);
  const deleteNodeByRfId = useShotWorkflowStore((s) => s.deleteNodeByRfId);
  const deleteEdgeByRfId = useShotWorkflowStore((s) => s.deleteEdgeByRfId);

  const { setCenter, getZoom, screenToFlowPosition } = useReactFlow();
  const sceneLabel = currentScene?.name ?? "";

  const [migrating, setMigrating] = useState(false);
  const [creatingShot, setCreatingShot] = useState(false);
  const [jumpOpen, setJumpOpen] = useState(false);
  const [jumpFilter, setJumpFilter] = useState("");
  // Right-click context menu (precise per-position add + shot actions).
  const [ctxMenu, setCtxMenu] = useState<
    { clientX: number; clientY: number; flowX: number; flowY: number; shotId: string | null } | null
  >(null);
  const migrateAttempted = useRef<string | null>(null);

  // Which shot frame (if any) contains a flow-space point — drives shot
  // assignment for toolbar placement + right-click add.
  const getShotAtFlow = useCallback(
    (fx: number, fy: number): { shotId: string; position: { x: number; y: number } } | null => {
      const groups = useShotWorkflowStore.getState().shotGroups;
      const byShot = groupChildren(useShotWorkflowStore.getState().nodes);
      // Topmost-first (later/lower groups don't matter; pick the first hit).
      for (const g of groups) {
        const { w, h } = groupSize(g, byShot.get(g.shot_id) ?? []);
        if (fx >= g.position.x && fx <= g.position.x + w && fy >= g.position.y && fy <= g.position.y + h) {
          return { shotId: g.shot_id, position: g.position };
        }
      }
      return null;
    },
    [],
  );

  const placeNodeAtFlow = useCallback(
    async (type: NodeType, fx: number, fy: number) => {
      const hit = getShotAtFlow(fx, fy);
      if (!hit) {
        useGenerationStore.setState({ error: "Click inside a shot frame to add a node." });
        return;
      }
      await useShotWorkflowStore
        .getState()
        .addNodeToShot(hit.shotId, type, { x: fx - hit.position.x, y: fy - hit.position.y });
    },
    [getShotAtFlow],
  );

  // Cmd/Ctrl+K → open the jump-to-shot palette.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setJumpFilter("");
        setJumpOpen((o) => !o);
      } else if (e.key === "Escape") {
        setJumpOpen(false);
        setCtxMenu(null);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  function jumpToShot(g: ShotGroup) {
    setJumpOpen(false);
    setCenter(g.position.x + MIN_W / 2, g.position.y + MIN_H / 2, { duration: 400, zoom: getZoom() });
  }

  // Load scene + canvas on mount / scene change.
  useEffect(() => {
    if (projectId && projectId !== currentProjectId) void selectProject(projectId);
  }, [projectId, currentProjectId, selectProject]);

  useEffect(() => {
    migrateAttempted.current = null;
    void selectScene(sceneId);
    void loadSceneCanvas(sceneId);
  }, [sceneId, selectScene, loadSceneCanvas]);

  // One-time auto-migrate when an existing scene has no shot_groups yet.
  useEffect(() => {
    if (loading) return;
    if (shotGroups.length > 0) return;
    if (migrateAttempted.current === sceneId) return;
    if (storeNodes.length === 0) {
      // No nodes → still attempt once so brand-new shots get a group too.
      migrateAttempted.current = sceneId;
    }
    migrateAttempted.current = sceneId;
    setMigrating(true);
    void autoMigrateScene(sceneId)
      .then(() => loadSceneCanvas(sceneId))
      .finally(() => setMigrating(false));
  }, [loading, shotGroups.length, storeNodes.length, sceneId, loadSceneCanvas]);

  // Reflow the vertical stack so consecutive groups maintain a constant
  // SHOT_VERTICAL_GAP gap regardless of each group's height. Anchors on the
  // first group's current (x, y) so a user-positioned top group stays put;
  // every following group's y is recomputed = prev.y + prev.h + GAP.
  // Called after delete (shifts up), manual resize (shifts down/up), and
  // anywhere a height delta could open/close a gap.
  const reflowStack = useCallback(() => {
    const groups = useShotWorkflowStore.getState().shotGroups;
    const nodes = useShotWorkflowStore.getState().nodes;
    const byShot = groupChildren(nodes);
    const ordered = [...groups].sort((a, b) => a.order - b.order);
    if (ordered.length === 0) return;
    const x = ordered[0].position.x ?? SHOT_DEFAULT_X;
    let y = ordered[0].position.y ?? 100;
    for (const g of ordered) {
      const { h } = groupSize(g, byShot.get(g.shot_id) ?? []);
      if (g.position.x !== x || g.position.y !== y) {
        const pos = { x, y };
        useShotWorkflowStore.getState().updateShotGroupLocal(g.shot_id, { position: pos });
        void patchShotGroup(g.shot_id, { position: pos }).catch(() => {});
      }
      y += h + SHOT_VERTICAL_GAP;
    }
  }, []);

  // RF-controlled local node array, seeded from the store (flat scene nodes
  // + shot_groups → group containers + parented children). Re-seed whenever
  // the authoritative store changes (load, generation update, group move).
  // Delete a shot: backend cascades nodes/edges + drops its group entry;
  // then reload + reflow the remaining shots up so no gap is left behind.
  const handleDeleteShot = useCallback(
    async (shotId: string) => {
      if (!window.confirm("Delete this shot and all its nodes? This can't be undone.")) return;
      try {
        await deleteShotApi(shotId);
      } catch {
        useGenerationStore.setState({ error: "Failed to delete shot" });
        return;
      }
      await loadSceneCanvas(sceneId);
      reflowStack();
    },
    [sceneId, loadSceneCanvas, reflowStack],
  );

  // Palette add: drop the node into the shot under the viewport center
  // (the old one-click-to-center behavior), or toast if center is empty.
  const handleAddFromPalette = useCallback(
    (type: NodeType) => {
      const f = screenToFlowPosition({ x: window.innerWidth / 2, y: window.innerHeight / 2 });
      void placeNodeAtFlow(type, f.x, f.y);
    },
    [screenToFlowPosition, placeNodeAtFlow],
  );

  const [rfNodes, setRfNodes] = useState<FlowNode[]>([]);
  useEffect(() => {
    setRfNodes(buildRfNodes(storeNodes, shotGroups, sceneLabel, handleDeleteShot, reflowStack));
  }, [storeNodes, shotGroups, sceneLabel, handleDeleteShot, reflowStack]);

  // When auto-fit changes a group's height (child added/removed, or any size
  // recompute), the next group's y can drift out of the constant-gap layout.
  // Detect height changes via a signature and reflow once when it shifts.
  const lastHeightsKey = useRef<string>("");
  useEffect(() => {
    const byShot = groupChildren(storeNodes);
    const ordered = [...shotGroups].sort((a, b) => a.order - b.order);
    const key = ordered
      .map((g) => `${g.shot_id}:${groupSize(g, byShot.get(g.shot_id) ?? []).h}`)
      .join("|");
    if (lastHeightsKey.current && lastHeightsKey.current !== key) reflowStack();
    lastHeightsKey.current = key;
  }, [storeNodes, shotGroups, reflowStack]);

  // Cross-shot edges (source & target in different shots) get a dashed accent
  // style so they read as inter-shot links (useful for Phase 8.4 later).
  const rfEdges = useMemo(() => {
    const shotOf = new Map(storeNodes.map((n) => [n.id, n.data.shotId]));
    return edges.map((e) => {
      const a = shotOf.get(e.source);
      const b = shotOf.get(e.target);
      if (a && b && a !== b) {
        return {
          ...e,
          className: "edge--cross-shot",
          style: { stroke: "var(--accent)", strokeWidth: 2, strokeDasharray: "6 4" },
        };
      }
      return e;
    });
  }, [edges, storeNodes]);

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    setRfNodes((prev) => applyNodeChanges(changes, prev) as FlowNode[]);
  }, []);

  // Connect two nodes (drag handle → handle). Scene-aware: the edge is owned
  // by the source node's shot.
  const onConnect = useCallback((c: Connection) => {
    if (c.source && c.target && c.source !== c.target) {
      void useShotWorkflowStore.getState().addSceneEdge(c.source, c.target);
    }
  }, []);

  const onNodeDragStop: OnNodeDrag<FlowNode> = useCallback((_e, node) => {
    if (node.id.startsWith(GROUP_PREFIX)) {
      const shotId = (node.data as { shotId: string }).shotId;
      useShotWorkflowStore.getState().updateShotGroupLocal(shotId, { position: node.position });
      void patchShotGroup(shotId, { position: node.position }).catch(() => {});
      // Reorder: keep the `order` field in sync with vertical position so a
      // drag that moves a shot above/below another updates ordering too.
      const groups = useShotWorkflowStore
        .getState()
        .shotGroups.slice()
        .sort((a, b) => a.position.y - b.position.y);
      groups.forEach((g, i) => {
        if (g.order !== i) {
          useShotWorkflowStore.getState().updateShotGroupLocal(g.shot_id, { order: i });
          void patchShotGroup(g.shot_id, { order: i }).catch(() => {});
        }
      });
    } else {
      // Persist to backend AND mirror into the store so the next re-seed
      // keeps the moved position (otherwise it'd snap back to the loaded x,y).
      void useShotWorkflowStore.getState().persistNodePosition(node.id, node.position);
      setNodesInStore(
        useShotWorkflowStore
          .getState()
          .nodes.map((n) => (n.id === node.id ? { ...n, position: node.position } : n)),
      );
    }
  }, [setNodesInStore]);

  // Delete (Backspace/Delete) — persist per node/edge. Group frames are
  // deletable:false so the key never drops a shot (that's the ✕ button).
  const onNodesDelete = useCallback(
    (deleted: Node[]) => {
      deleted.forEach((n) => {
        if (!n.id.startsWith(GROUP_PREFIX)) void deleteNodeByRfId(n.id);
      });
    },
    [deleteNodeByRfId],
  );

  const onEdgesDelete = useCallback(
    (deleted: { id: string }[]) => {
      deleted.forEach((e) => void deleteEdgeByRfId(e.id));
    },
    [deleteEdgeByRfId],
  );

  const onNodeDoubleClick = useCallback((_e: React.MouseEvent, node: Node) => {
    if (node.id.startsWith(GROUP_PREFIX)) return;
    const data = node.data as FlowNode["data"];
    if (!["image", "prompt", "video", "visual_asset", "character"].includes(data.type)) return;
    const s = useGenerationStore.getState();
    if (data.mediaId) s.openResultViewer(node.id);
    else s.openGenerationDialog(node.id, data.prompt ?? "");
  }, []);

  // ── Right-click context menu (on the flow wrapper) ──
  const onWrapperContextMenu = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      const f = screenToFlowPosition({ x: e.clientX, y: e.clientY });
      const hit = getShotAtFlow(f.x, f.y);
      setCtxMenu({ clientX: e.clientX, clientY: e.clientY, flowX: f.x, flowY: f.y, shotId: hit?.shotId ?? null });
    },
    [screenToFlowPosition, getShotAtFlow],
  );

  // Close the context menu on any plain click.
  useEffect(() => {
    if (!ctxMenu) return;
    const close = () => setCtxMenu(null);
    document.addEventListener("click", close);
    return () => document.removeEventListener("click", close);
  }, [ctxMenu]);

  async function handleNewShot() {
    if (creatingShot) return;
    setCreatingShot(true);
    try {
      const groups = useShotWorkflowStore.getState().shotGroups;
      const nodes = useShotWorkflowStore.getState().nodes;
      // Smart placement: stack the new shot just below the lowest existing
      // group (not a fixed far stride), aligned to the column x.
      const pos = nextShotPosition(groups, nodes);
      const shot = await createShot(sceneId);
      if (!shot) return;
      // patchShotGroup creates the group entry if missing → no auto-migrate
      // stride; the new frame lands exactly where we computed.
      await patchShotGroup(shot.id, {
        position: pos,
        label: `Shot ${groups.length + 1}`,
        collapsed: false,
        order: groups.length,
      }).catch(() => {});
      await loadSceneCanvas(sceneId);
      // Auto-pan so the user immediately sees the new (empty) shot frame.
      setCenter(pos.x + MIN_W / 2, pos.y + MIN_H / 2, { duration: 500, zoom: getZoom() });
    } finally {
      setCreatingShot(false);
    }
  }

  return (
    <div className="page page--scene-canvas">
      <header className="page-header page-header--canvas">
        <nav className="breadcrumb" aria-label="Breadcrumb">
          <Link to="/projects">Projects</Link>
          <span aria-hidden="true">/</span>
          <Link to={`/projects/${projectId}`}>{currentProject?.name ?? "…"}</Link>
          <span aria-hidden="true">/</span>
          <span>{currentScene?.name ?? "Scene"}</span>
        </nav>
        <button
          type="button"
          className="btn btn--primary"
          onClick={() => void handleNewShot()}
          disabled={creatingShot}
        >
          {creatingShot ? "Adding…" : "+ New Shot"}
        </button>
      </header>

      {migrating && (
        <div className="scene-canvas__banner" role="status">
          ⏳ Migrating to multi-shot canvas…
        </div>
      )}

      {jumpOpen && (
        <div className="jump-modal-backdrop" role="presentation" onClick={() => setJumpOpen(false)}>
          <div className="jump-modal" role="dialog" aria-label="Jump to shot" onClick={(e) => e.stopPropagation()}>
            <input
              className="jump-modal__input"
              autoFocus
              placeholder="Jump to shot… (type a shot name)"
              value={jumpFilter}
              onChange={(e) => setJumpFilter(e.target.value)}
            />
            <ul className="jump-modal__list">
              {[...shotGroups]
                .sort((a, b) => a.order - b.order)
                .filter((g) =>
                  jumpFilter.trim()
                    ? `${g.label} ${sceneLabel}`.toLowerCase().includes(jumpFilter.trim().toLowerCase())
                    : true,
                )
                .map((g) => (
                  <li key={g.shot_id}>
                    <button type="button" className="jump-modal__item" onClick={() => jumpToShot(g)}>
                      <span className="jump-modal__item-label">{g.label}</span>
                      <span className="jump-modal__item-scene">{sceneLabel}</span>
                    </button>
                  </li>
                ))}
              {shotGroups.length === 0 && (
                <li className="jump-modal__empty">No shots in this scene.</li>
              )}
            </ul>
          </div>
        </div>
      )}

      <div className="scene-canvas__flow" onContextMenu={onWrapperContextMenu}>
        {!loading && !migrating && shotGroups.length === 0 && (
          <div className="scene-canvas__empty">
            No shots yet. Use <strong>+ New Shot</strong> to create your first shot.
          </div>
        )}
        <SceneCanvasToolbar onAdd={handleAddFromPalette} />
        <ReactFlow
          nodes={rfNodes}
          edges={rfEdges}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          onNodesChange={onNodesChange}
          onNodeDragStop={onNodeDragStop}
          onNodeDoubleClick={onNodeDoubleClick}
          onConnect={onConnect}
          onNodesDelete={onNodesDelete}
          onEdgesDelete={onEdgesDelete}
          connectionRadius={32}
          defaultEdgeOptions={{ style: { stroke: "var(--border)", strokeWidth: 2 } }}
          // Backspace/Delete removes the selected node/edge (persisted via
          // onNodesDelete/onEdgesDelete). Shot frames are deletable:false →
          // the key can't drop a shot; that stays on the header ✕.
          deleteKeyCode={["Backspace", "Delete"]}
          fitView
          colorMode="dark"
          proOptions={{ hideAttribution: true }}
        >
          <Background variant={BackgroundVariant.Dots} gap={24} size={1} color="#2a2e38" />
          <MiniMap pannable zoomable />
          <Controls />
        </ReactFlow>
      </div>

      {/* Right-click context menu (add node + shot actions if inside a shot). */}
      {ctxMenu && (
        <div
          className="canvas-ctx-menu"
          style={{ left: ctxMenu.clientX, top: ctxMenu.clientY }}
          role="menu"
          onClick={(e) => e.stopPropagation()}
        >
          {SCENE_NODE_TYPES.map((t) => (
            <button
              key={t.type}
              type="button"
              role="menuitem"
              onClick={() => {
                void placeNodeAtFlow(t.type, ctxMenu.flowX, ctxMenu.flowY);
                setCtxMenu(null);
              }}
            >
              <span aria-hidden="true">{t.icon}</span> Add {t.label}
            </button>
          ))}
          {ctxMenu.shotId && (
            <>
              <div className="canvas-ctx-menu__divider" />
              <button
                type="button"
                role="menuitem"
                className="canvas-ctx-menu__danger"
                onClick={() => {
                  const sid = ctxMenu.shotId!;
                  setCtxMenu(null);
                  void handleDeleteShot(sid);
                }}
              >
                ✕ Delete shot
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Phase 8.3 multi-shot SceneCanvas: all of a scene's shots in one React Flow
 * canvas, each shot a draggable/collapsible group frame (RF parent/child).
 * Double-click a node → generate/view (reuses the existing dialog + dispatch,
 * which read the merged scene graph from useShotWorkflowStore).
 */
export function SceneCanvas() {
  const { projectId, sceneId } = useParams<{ projectId: string; sceneId: string }>();
  if (!projectId || !sceneId) {
    return <div className="page-empty">Missing project or scene id in URL.</div>;
  }
  return (
    <ReactFlowProvider>
      <SceneCanvasInner projectId={projectId} sceneId={sceneId} />
    </ReactFlowProvider>
  );
}
