import assert from 'node:assert/strict';
import test from 'node:test';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const bundle = await build({ entryPoints: [fileURLToPath(new URL('../src/automation/clipCanvasLayout.ts', import.meta.url))],
  bundle: true, platform: 'node', format: 'cjs', write: false, logLevel: 'silent' });
const mod = { exports: {} };
new Function('require', 'module', 'exports', bundle.outputFiles[0].text)(createRequire(import.meta.url), mod, mod.exports);
const { projectClipCanvas, saveClipPositions, frameId, childId } = mod.exports;

function fixture() {
  const actor = { id: 'char:hero', data: { kind: 'character' }, position: { x: 0, y: 10 } };
  const prop = { id: 'asset:gift', data: { kind: 'asset' }, position: { x: 400, y: 10 } };
  const extra = { id: 'asset:unused', data: { kind: 'asset' }, position: { x: 400, y: 400 } };
  const nodes = [actor, prop, extra], edges = [], groups = [];
  for (const [i, key] of ['clip:01', 'clip:02'].entries()) {
    const sequence = { id: `seq:${key}`, data: { kind: 'sequence', sequence: { key }, shots: [] }, position: { x: 1000, y: i * 1000 } };
    const video = { id: `vid:${key}`, data: { kind: 'video', refs: ['original ref'], prompt: 'Original prompt' }, position: { x: 1500, y: i * 1000 } };
    nodes.push(sequence, video);
    edges.push({ id: `edge:${key}`, source: actor.id, target: video.id });
    groups.push({ key, label: key, title: 'Test', sequence, video,
      materials: [actor, prop].map(node => ({ id: node.id, node, name: node.id, kind: 'character', previews: [], usedBy: 2 })) });
  }
  return { nodes, edges, groups };
}

test('frames own draggable children and shared materials have a separate canvas instance in each frame', () => {
  const { nodes, edges, groups } = fixture();
  const result = projectClipCanvas(groups, nodes, edges);
  for (const group of groups) {
    const frame = result.nodes.find(n => n.id === frameId(group.key));
    assert.equal(frame.dragHandle, '.clip-canvas-frame__head');
    assert.equal(frame.draggable, true);
    for (const child of result.nodes.filter(n => n.parentId === frame.id)) {
      assert.equal(child.extent, 'parent');
      assert.equal(child.draggable, true);
      assert.ok(result.nodes.indexOf(frame) < result.nodes.indexOf(child));
    }
    assert.equal(result.bindings.get(childId(group.key, 'char:hero')).sourceId, 'char:hero');
  }
  assert.equal(new Set(result.nodes.map(n => n.id)).size, result.nodes.length);
  assert.ok(result.nodes.some(n => n.id === 'asset:unused'));
  assert.ok(result.edges.every(e => result.bindings.has(e.source) && result.bindings.has(e.target)));
});

test('moving a frame persists its offset without changing film order, references or the other frame', () => {
  const { nodes, edges, groups } = fixture();
  const initial = projectClipCanvas(groups, nodes, edges);
  const position = { x: 620, y: 2400 };
  const updated = saveClipPositions(nodes, [{ id: frameId(groups[0].key), position }], initial.bindings);
  const next = projectClipCanvas(groups, updated, edges);
  assert.deepEqual(next.nodes.find(n => n.id === frameId(groups[0].key)).position, position);
  assert.deepEqual(next.nodes.find(n => n.id === frameId(groups[1].key)).position, initial.nodes.find(n => n.id === frameId(groups[1].key)).position);
  for (let i = 0; i < nodes.length; i++) {
    assert.strictEqual(updated[i].data, nodes[i].data);
    assert.deepEqual(updated[i].position, nodes[i].position);
  }
});

test('dragging one shared material moves only that instance; saved positions survive JSON roundtrip', () => {
  const { nodes, edges, groups } = fixture();
  const initial = projectClipCanvas(groups, nodes, edges);
  const movingId = childId(groups[0].key, 'char:hero');
  const position = { x: 75, y: 120 };
  const updated = saveClipPositions(nodes, [{ id: movingId, position }], initial.bindings);
  const next = projectClipCanvas(groups, JSON.parse(JSON.stringify(updated)), edges);
  assert.deepEqual(next.nodes.find(n => n.id === movingId).position, position);
  for (const node of initial.nodes.filter(n => n.parentId && n.id !== movingId)) {
    assert.deepEqual(next.nodes.find(n => n.id === node.id).position, node.position);
  }
});

test('frames expand to contain measured content and saved child offsets', () => {
  const { nodes, edges, groups } = fixture();
  const initial = projectClipCanvas(groups, nodes, edges);
  const id = childId(groups[0].key, groups[0].video.id);
  const updated = saveClipPositions(nodes, [{ id, position: { x: 1500, y: 900 } }], initial.bindings);
  const next = projectClipCanvas(groups, updated, edges, { [id]: { width: 360, height: 1300 } });
  const frame = next.nodes.find(n => n.id === frameId(groups[0].key));
  assert.ok(frame.width >= 1860);
  assert.ok(frame.height >= 2200);
});

test('multiple selected node/frame moves are merged on the correct owners', () => {
  const { nodes, edges, groups } = fixture();
  const initial = projectClipCanvas(groups, nodes, edges);
  const moved = [
    { id: frameId(groups[0].key), position: { x: 600, y: 40 } },
    { id: childId(groups[0].key, 'char:hero'), position: { x: 24, y: 108 } },
    { id: frameId(groups[1].key), position: { x: 600, y: 1100 } },
  ];
  const updated = saveClipPositions(nodes, moved, initial.bindings);
  assert.deepEqual(updated.find(n => n.id === groups[0].sequence.id).clipLayout.$frame, moved[0].position);
  assert.deepEqual(updated.find(n => n.id === groups[0].sequence.id).clipLayout['char:hero'], moved[1].position);
  assert.deepEqual(updated.find(n => n.id === groups[1].sequence.id).clipLayout.$frame, moved[2].position);
});

test('unassigned materials stay outside frames and keep an independent saved canvas position', () => {
  const { nodes, edges, groups } = fixture();
  const original = nodes.find(n => n.id === 'asset:unused');
  const initial = projectClipCanvas(groups, nodes, edges);
  assert.equal(initial.nodes.find(n => n.id === original.id).position.x, 0);
  const position = { x: -420, y: 320 };
  const updated = saveClipPositions(nodes, [{ id: original.id, position }], initial.bindings);
  const next = projectClipCanvas(groups, updated, edges);
  assert.deepEqual(next.nodes.find(n => n.id === original.id).position, position);
  assert.deepEqual(updated.find(n => n.id === original.id).position, original.position);
});
