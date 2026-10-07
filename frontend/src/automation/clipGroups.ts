import type { Edge, Node } from "@xyflow/react";
import type { AutoNode, Plate, SequenceNodeData, VideoNodeData } from "../store/automation";
import type { AssetKind, ProductionAsset } from "./contracts";

export interface ClipMaterial {
  id: string;
  name: string;
  kind: AssetKind | "reference" | "keyframe";
  node?: AutoNode;
  previews: { key: string; name: string; plate: Plate }[];
  usedBy: number;
}

export interface ClipGroup {
  key: string;
  label: string;
  title: string;
  sequence?: Node<SequenceNodeData>;
  video?: Node<VideoNodeData>;
  materials: ClipMaterial[];
}

export function isMaterialNode(node: AutoNode) {
  return ["character", "environment", "asset"].includes(node.data.kind);
}

export function materialFromNode(node: AutoNode, shots: SequenceNodeData["shots"] = []): ClipMaterial {
  const d = node.data;
  if (d.kind === "character") {
    const states = d.states ?? {};
    const definitions = d.character.states ?? [];
    const stateKeys = [...new Set(shots.map(s => s.character_states?.[d.character.key]).filter((s): s is string => Boolean(s)))];
    if (!stateKeys.length && d.activeState) stateKeys.push(d.activeState);
    return {
      id: node.id, node, kind: "character", name: d.character.name, usedBy: 1,
      previews: stateKeys.length ? stateKeys.map(key => ({
        key, name: definitions.find(s => s.key === key)?.label || key,
        // Never substitute another costume just because its tab is selected.
        plate: states[key]?.referenceUrl || states[key]?.image ? states[key]
          : definitions.length <= 1 ? d.identity : states[key] ?? { prompt: "", status: "idle" },
      })) : [{ key: "identity", name: "Nhận dạng", plate: d.identity }],
    };
  }
  if (d.kind === "environment") return {
    id: node.id, node, kind: "environment", name: d.environment.name, usedBy: 1,
    previews: [{ key: "plate", name: "Bối cảnh", plate: d.plate }],
  };
  if (d.kind === "asset") return {
    id: node.id, node, kind: d.asset.kind, name: d.asset.name, usedBy: 1,
    previews: [{ key: "plate", name: d.asset.kind === "prop" ? "Đạo cụ" : "Quần chúng", plate: d.plate }],
  };
  throw new Error(`Not a material: ${node.id}`);
}

/** A display-only projection. Nodes, reference order and production contracts
 * remain canonical; shared materials are never cloned into the saved board. */
export function buildClipGroups(nodes: AutoNode[], edges: Edge[], assets: ProductionAsset[] = []): ClipGroup[] {
  const sequences = nodes.filter((n): n is Node<SequenceNodeData> => n.data.kind === "sequence")
    .sort((a, b) => a.position.y - b.position.y);
  const videos = nodes.filter((n): n is Node<VideoNodeData> => n.data.kind === "video");
  const groups: ClipGroup[] = sequences.map(sequence => ({
    key: sequence.data.sequence.key, label: sequence.data.sequence.label, title: sequence.data.sequence.title,
    sequence, video: videos.find(v => v.data.sequenceKey === sequence.data.sequence.key), materials: [],
  }));
  // Older imports can have a video but no sequence. Keep it accessible.
  for (const video of videos) if (!groups.some(g => g.key === video.data.sequenceKey)) {
    groups.push({ key: video.data.sequenceKey, label: video.data.label, title: video.data.title, video, materials: [] });
  }
  for (const group of groups) {
    const seq = group.sequence?.data.sequence;
    const shots = group.sequence?.data.shots ?? [];
    const refs = group.video?.data.refs ?? [];
    const characters = new Set([...(seq?.character_keys ?? []), ...shots.flatMap(s => s.character_keys ?? [])]);
    const environments = new Set([seq?.environment_key, ...shots.map(s => s.environment_key)].filter(Boolean));
    const needed = new Set([
      ...(seq?.asset_keys ?? []), ...(seq?.scene_present_asset_ids ?? []),
      ...Object.values(seq?.shot_package?.materials ?? {}).map(m => m.asset_id),
      ...shots.flatMap(s => [...(s.scene_present_asset_ids ?? []), ...(s.asset_presence ?? []).map(p => p.asset_id)]),
      ...refs.flatMap(r => [r.assetId, ...(r.assetBindings ?? []).map(b => b.assetId)].filter((id): id is string => Boolean(id))),
    ]);
    let changed = true;
    while (changed) {
      changed = false;
      for (const asset of assets) if (needed.has(asset.id) || (asset.production_key && needed.has(asset.production_key))) {
        for (const id of [...(asset.member_ids ?? []), ...(asset.depends_on_asset_ids ?? [])]) {
          if (!needed.has(id)) { needed.add(id); changed = true; }
        }
      }
    }
    const incoming = new Set(edges.filter(e => e.target === group.sequence?.id || e.target === group.video?.id).map(e => e.source));
    const resolved = new Set<string>();
    const linkedCharacters = new Set<string>();
    const linkedEnvironments = new Set<string>();
    for (const node of nodes) {
      if (!isMaterialNode(node)) continue;
      const d = node.data;
      const keys = d.kind === "character" ? [d.character.key, d.character.source_asset_id]
        : d.kind === "environment" ? [d.environment.key, d.environment.source_asset_id]
        : d.kind === "asset" ? [d.asset.key, d.asset.id] : [];
      const material = materialFromNode(node, shots);
      const plates = d.kind === "character" ? [d.identity, ...Object.values(d.states ?? {})] : material.previews.map(p => p.plate);
      const linked = incoming.has(node.id) || keys.some(key => key && needed.has(key))
        || (d.kind === "character" && characters.has(d.character.key))
        || (d.kind === "environment" && environments.has(d.environment.key))
        // URL fallback is only for legacy refs without explicit identities.
        || refs.some(r => !r.assetId && !r.assetBindings?.length && plates.some(p => r.url && (r.url === p.referenceUrl || r.url === p.image)));
      if (!linked) continue;
      group.materials.push(material);
      keys.forEach(key => { if (key) resolved.add(key); });
      if (d.kind === "character") linkedCharacters.add(d.character.key);
      if (d.kind === "environment") linkedEnvironments.add(d.environment.key);
    }
    for (const key of characters) if (!linkedCharacters.has(key)) {
      group.materials.push({ id: `missing-character:${key}`, name: key, kind: "character", previews: [], usedBy: 1 });
      resolved.add(key);
    }
    for (const key of environments) if (key && !linkedEnvironments.has(key)) {
      group.materials.push({ id: `missing-environment:${key}`, name: key, kind: "environment", previews: [], usedBy: 1 });
      resolved.add(key);
    }
    for (const id of needed) if (!resolved.has(id)) {
      const asset = assets.find(a => a.id === id || a.production_key === id);
      const ref = refs.find(r => r.assetId === id || r.assetBindings?.some(b => b.assetId === id));
      group.materials.push({ id: `reference:${id}`, name: asset?.production_name || asset?.name || ref?.name || id,
        kind: asset?.kind || ref?.kind || "reference", usedBy: 1,
        previews: ref?.url ? [{ key: ref.label, name: ref.label, plate: { prompt: "", status: "done", referenceUrl: ref.url } }] : [] });
    }
    // Include standalone imported refs/storyboards, without inventing a node.
    for (const ref of refs) if (!ref.assetId && !ref.assetBindings?.length
      && !group.materials.some(m => m.previews.some(p => (p.plate.referenceUrl || p.plate.image) === ref.url))) {
      group.materials.push({ id: `url:${ref.url}`, name: ref.name, kind: "reference", usedBy: 1,
        previews: [{ key: ref.label, name: ref.label, plate: { prompt: "", status: "done", referenceUrl: ref.url } }] });
    }
    const video = group.video?.data;
    const frames = video ? [["start", "Frame đầu", video.startFrame], ["end", "Frame cuối", video.endFrame],
      ...Object.entries(video.shotFrames ?? {}).map(([key, plate]) => [key, `Keyframe ${key}`, plate])] as [string, string, Plate | undefined][] : [];
    for (const [key, name, plate] of frames) if (plate) group.materials.push({
      id: `frame:${group.key}:${key}`, name, kind: "keyframe", usedBy: 1,
      previews: [{ key, name, plate }],
    });
  }
  const counts = new Map<string, number>();
  for (const group of groups) for (const id of new Set(group.materials.map(m => m.id))) counts.set(id, (counts.get(id) ?? 0) + 1);
  for (const group of groups) for (const material of group.materials) material.usedBy = counts.get(material.id) ?? 1;
  return groups;
}
