import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { applyNodeChanges, Background, BackgroundVariant, Controls, Handle, MiniMap, Panel, Position,
  ReactFlow, SelectionMode, useStore, useReactFlow, type NodeChange, type NodeProps,
  type OnNodeDrag, type Connection, type EdgeChange } from "@xyflow/react";
import { useAutomation } from "../store/automation";
import { buildClipGroups, type ClipMaterial } from "./clipGroups";
import { frameId, projectClipCanvas, saveClipPositions, type CanvasNode, type Measurements } from "./clipCanvasLayout";
import { AutoSequenceNode, AutoVideoNode, EmbeddedAutomationNodes, automationNodeTypes } from "./nodes";
import { MaterialCard, MaterialEditor } from "./ClipMaterial";

function ClipFrameNode({ data }: NodeProps) {
  return <div className="clip-canvas-frame">
    <header className="clip-canvas-frame__head">
      <strong>{String(data.label)} — {String(data.title)}</strong>
      <span>{String(data.shots)} shot · {String(data.materials)} material · Kéo thanh này để di chuyển cả khung</span>
    </header>
  </div>;
}

function ClipMaterialNode({ data }: NodeProps) {
  const material = data.material as ClipMaterial;
  return <>
    <MaterialCard material={material} onEdit={data.onEdit as (id: string) => void} />
    {material.node && <Handle type="source" position={Position.Right} className="auto-handle" />}
  </>;
}

function ClipContentNode({ data, selected }: NodeProps) {
  const node = useAutomation(s => s.nodes.find(n => n.id === data.sourceId));
  if (!node) return null;
  const props = { id: node.id, data: node.data, selected };
  return node.data.kind === "sequence" ? <AutoSequenceNode {...props} />
    : node.data.kind === "video" ? <AutoVideoNode {...props} /> : null;
}

const nodeTypes = { ...automationNodeTypes, clipFrame: ClipFrameNode, clipMaterial: ClipMaterialNode, clipContent: ClipContentNode };

function FrameNavigation({ groups }: { groups: { key: string; label: string; title: string }[] }) {
  const { fitView, fitBounds, getNode } = useReactFlow();
  const target = groups.length ? frameId(groups[0].key) : "script";
  const ready = useStore(s => {
    const node = s.nodeLookup.get(target);
    return Boolean(node?.measured.width && node?.measured.height);
  });
  const fitted = useRef(false);
  useEffect(() => {
    if (ready && !fitted.current) {
      fitted.current = true;
      void fitView({ nodes: [{ id: target }], padding: .12, maxZoom: .85, minZoom: .2 });
    }
  }, [ready, target, fitView]);
  return <Panel position="top-right" className="clip-canvas-navigation nodrag nopan">
    <label>Đi tới clip <select aria-label="Đi tới clip" defaultValue="" onChange={e => {
      const node = getNode(e.target.value);
      if (node?.id === "script" && node.measured?.height) {
        void fitView({ nodes: [{ id: node.id }], padding: .12, duration: 180, maxZoom: 1 });
        return;
      }
      // Offscreen frames are virtualized and may not have DOM measurements yet.
      if (node) void fitBounds({ ...node.position, width: node.width ?? node.measured?.width ?? 380,
        height: node.height ?? node.measured?.height ?? 780 }, { padding: .12, duration: 180 });
    }}>
      <option value="" disabled>Chọn khung…</option>
      <option value="script">Video nguồn</option>
      {groups.map(g => <option key={g.key} value={frameId(g.key)}>{g.label} — {g.title}</option>)}
    </select></label>
  </Panel>;
}

export function ClipCanvas({ onSelection }: { onSelection?(ids: string[]): void }) {
  const nodes = useAutomation(s => s.nodes);
  const edges = useAutomation(s => s.edges);
  const assets = useAutomation(s => s.productionAssets);
  const [editorId, setEditorId] = useState<string | null>(null);
  const [measurements, setMeasurements] = useState<Measurements>({});
  const [displayNodes, setDisplayNodes] = useState<CanvasNode[]>([]);
  const groups = useMemo(() => buildClipGroups(nodes, edges, assets), [nodes, edges, assets]);
  const projection = useMemo(() => projectClipCanvas(groups, nodes, edges, measurements, setEditorId), [groups, nodes, edges, measurements]);
  const editorNode = nodes.find(n => n.id === editorId);
  useEffect(() => {
    setDisplayNodes(previous => {
      const before = new Map(previous.map(n => [n.id, n]));
      return projection.nodes.map(n => {
        const current = before.get(n.id);
        return { ...n, selected: current?.selected ?? false,
          ...(current?.dragging ? { position: current.position, dragging: true } : {}) };
      });
    });
  }, [projection]);

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    setDisplayNodes(previous => applyNodeChanges(changes, previous));
    setMeasurements(previous => {
      let next = previous;
      for (const change of changes) if (change.type === "dimensions" && change.dimensions && !change.id.startsWith("clip-frame:")) {
        const old = next[change.id];
        if (!old || old.width !== change.dimensions.width || old.height !== change.dimensions.height) {
          if (next === previous) next = { ...previous };
          next[change.id] = change.dimensions;
        }
      }
      return next;
    });
  }, []);

  const onDragStop: OnNodeDrag<CanvasNode> = useCallback((_event, node, dragged) => {
    const moved = dragged.length ? dragged : [node];
    useAutomation.setState(state => {
      const updated = saveClipPositions(state.nodes, moved, projection.bindings);
      return { nodes: updated.map(original => {
        const direct = moved.find(m => m.id === original.id && !projection.bindings.has(m.id));
        return direct ? { ...original, position: { ...direct.position } } : original;
      }) };
    });
  }, [projection.bindings]);

  const onConnect = useCallback((connection: Connection) => {
    const source = projection.bindings.get(connection.source)?.sourceId ?? connection.source;
    const target = projection.bindings.get(connection.target)?.sourceId ?? connection.target;
    if (nodes.some(n => n.id === source) && nodes.some(n => n.id === target))
      useAutomation.getState().onConnect({ ...connection, source, target });
  }, [projection.bindings, nodes]);
  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    const canonical = changes.filter(c => c.type === "remove").map(c => ({ ...c,
      id: String(projection.edges.find(e => e.id === c.id)?.data?.canonicalId ?? c.id) }));
    if (canonical.length) useAutomation.getState().onEdgesChange(canonical);
  }, [projection.edges]);

  const onSelectionChange = useCallback(({ nodes: selected }: { nodes: CanvasNode[] }) => onSelection?.([...new Set(selected.map(n => {
        const binding = projection.bindings.get(n.id);
        return binding?.sourceId ?? binding?.anchorId ?? n.id;
      }).filter(id => nodes.some(n => n.id === id)))]), [nodes, projection.bindings, onSelection]);

  return <div className="auto-canvas auto-canvas--clip-frames">
    <ReactFlow nodes={displayNodes} edges={projection.edges} nodeTypes={nodeTypes}
      onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onConnect={onConnect}
      onSelectionChange={onSelectionChange}
      onNodeDragStop={onDragStop} minZoom={.1} maxZoom={2}
      onSelectionDragStop={(event, moved) => { if (moved[0]) onDragStop(event, moved[0], moved); }}
      proOptions={{ hideAttribution: true }} selectionKeyCode="Alt" selectionMode={SelectionMode.Partial}
      multiSelectionKeyCode={["Meta", "Control", "Shift"]} panOnDrag selectNodesOnDrag={false}
      noDragClassName="nodrag" onlyRenderVisibleElements>
      <Background variant={BackgroundVariant.Dots} gap={22} size={1} />
      <FrameNavigation groups={groups} />
      <Controls showInteractive={false} />
      <MiniMap pannable zoomable />
    </ReactFlow>
    {editorNode && <EmbeddedAutomationNodes.Provider value={true}>
      <MaterialEditor node={editorNode} onClose={() => setEditorId(null)} />
    </EmbeddedAutomationNodes.Provider>}
  </div>;
}
