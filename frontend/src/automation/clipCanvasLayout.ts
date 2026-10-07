import type { Edge, Node, XYPosition } from "@xyflow/react";
import type { AutoNode } from "../store/automation";
import type { ClipGroup } from "./clipGroups";

export type CanvasNode = Node<Record<string, unknown>>;
export type Measurements = Record<string, { width: number; height: number }>;
export interface LayoutBinding { anchorId: string; slot: string; sourceId?: string }
export const frameId = (key: string) => `clip-frame:${JSON.stringify(key)}`;
export const childId = (key: string, source: string) => `clip-child:${JSON.stringify([key, source])}`;

export function projectClipCanvas(groups: ClipGroup[], originals: AutoNode[], originalEdges: Edge[],
  measurements: Measurements = {}, onEdit: (id: string) => void = () => {}) {
  const nodes: CanvasNode[] = [];
  const edges: Edge[] = [];
  const bindings = new Map<string, LayoutBinding>();
  const used = new Set<string>();
  const originalsById = new Map(originals.map(n => [n.id, n]));
  let nextY = 0;
  for (const group of groups) {
    const anchorId = group.sequence?.id ?? group.video!.id;
    const saved = originalsById.get(anchorId)?.clipLayout ?? {};
    const parentId = frameId(group.key);
    const children: CanvasNode[] = [];
    const aliases = new Map<string, string>();
    const columnY = [92, 92];
    function add(source: string, type: string, data: Record<string, unknown>, position: XYPosition,
      size: { width: number; height: number }, sourceId?: string) {
      const id = childId(group.key, source);
      const dimensions = measurements[id] ?? size;
      const chosen = saved[source] ?? position;
      const node: CanvasNode = { id, type, parentId, extent: "parent", deletable: false,
        position: { x: Math.max(20, chosen.x), y: Math.max(84, chosen.y) },
        draggable: true, connectable: Boolean(sourceId),
        // Drag any non-interactive surface, not just the narrow title text.
        style: type === "clipMaterial" ? { width: size.width } : undefined,
        data, measured: measurements[id] };
      children.push(node);
      bindings.set(id, { anchorId, slot: source, sourceId });
      if (sourceId) { aliases.set(sourceId, id); used.add(sourceId); }
      return { node, dimensions };
    }
    for (const [index, material] of group.materials.entries()) {
      const column = index % 2;
      const item = add(material.id, "clipMaterial", { material, onEdit }, { x: 24 + column * 300, y: columnY[column] },
        { width: 276, height: 240 + Math.max(0, material.previews.length - 1) * 180 }, material.node?.id);
      columnY[column] += item.dimensions.height + 24;
    }
    if (group.sequence) add(group.sequence.id, "clipContent", { sourceId: group.sequence.id, contentKind: "sequence" },
      { x: 644, y: 92 }, { width: 330, height: 350 }, group.sequence.id);
    if (group.video) add(group.video.id, "clipContent", { sourceId: group.video.id, contentKind: "video" },
      { x: 1016, y: 92 }, { width: 360, height: 740 }, group.video.id);
    let width = 1410, height = 510;
    for (const node of children) {
      const size = measurements[node.id] ?? { width: node.type === "clipMaterial" ? 276 : node.data.contentKind === "video" ? 360 : 330,
        height: node.type === "clipMaterial" ? 260 : node.data.contentKind === "video" ? 740 : 350 };
      width = Math.max(width, node.position.x + size.width + 32);
      height = Math.max(height, node.position.y + size.height + 32);
    }
    const position = saved.$frame ?? { x: 480, y: nextY };
    nextY += height + 100;
    bindings.set(parentId, { anchorId, slot: "$frame" });
    nodes.push({ id: parentId, type: "clipFrame", position, data: { label: group.label, title: group.title,
      materials: group.materials.length, shots: group.sequence?.data.shots.length ?? 0 },
      style: { width, height }, width, height, dragHandle: ".clip-canvas-frame__head", deletable: false,
      connectable: false, draggable: true, zIndex: -1 }, ...children);
    for (const edge of originalEdges) {
      const source = aliases.get(edge.source), target = aliases.get(edge.target);
      if (source && target) edges.push({ ...edge, id: `clip-edge:${group.key}:${edge.id}`, source, target,
        data: { ...edge.data, canonicalId: edge.id }, zIndex: 0 });
    }
  }
  // Source input and unassigned materials stay in their own lane, rather than
  // overlapping frames at their legacy canvas positions.
  let standaloneY = 0;
  for (const node of originals) if (!used.has(node.id) && !["sequence", "video"].includes(node.data.kind)) {
    const measured = measurements[node.id] ?? node.measured;
    if (groups.length) {
      const position = node.clipLayout?.$standalone ?? { x: 0, y: standaloneY };
      nodes.push({ ...node, position, measured });
      bindings.set(node.id, { anchorId: node.id, slot: "$standalone", sourceId: node.id });
      standaloneY += (measurements[node.id]?.height ?? 780) + 80;
    } else nodes.push({ ...node, measured });
  }
  const ids = new Set(nodes.map(n => n.id));
  for (const edge of originalEdges) if (ids.has(edge.source) && ids.has(edge.target)) edges.push(edge);
  return { nodes, edges, bindings };
}

/** Save just display positions on the owning sequence/video. Keep canonical
 * position.y (film order), data objects, refs and IDs completely unchanged. */
export function saveClipPositions(originals: AutoNode[], moved: Pick<CanvasNode, "id" | "position">[], bindings: Map<string, LayoutBinding>) {
  const updates = new Map<string, Record<string, XYPosition>>();
  for (const node of moved) {
    const binding = bindings.get(node.id);
    if (!binding) continue;
    const update = updates.get(binding.anchorId) ?? {};
    update[binding.slot] = { ...node.position };
    updates.set(binding.anchorId, update);
  }
  return originals.map(node => updates.has(node.id)
    ? { ...node, clipLayout: { ...node.clipLayout, ...updates.get(node.id) } } : node);
}
