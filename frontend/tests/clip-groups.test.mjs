import assert from 'node:assert/strict';
import test from 'node:test';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const bundle = await build({ entryPoints: [fileURLToPath(new URL('../src/automation/clipGroups.ts', import.meta.url))],
  bundle: true, platform: 'node', format: 'cjs', write: false, logLevel: 'silent' });
const mod = { exports: {} };
new Function('require', 'module', 'exports', bundle.outputFiles[0].text)(createRequire(import.meta.url), mod, mod.exports);
const { buildClipGroups } = mod.exports;
const plate = key => ({ prompt: key, status: 'done', referenceUrl: `https://assets.test/${key}.png` });
const char = key => ({ id: `char:${key}`, position: { x: 0, y: 0 }, data: { kind: 'character',
  character: { key, source_asset_id: `source-${key}`, name: key, states: [{ key: 'day', label: 'Day' }, { key: 'night', label: 'Night' }] },
  identity: plate(key), states: { day: plate(`${key}-day`), night: plate(`${key}-night`) }, activeState: 'night' } });
const env = key => ({ id: `env:${key}`, position: { x: 0, y: 0 }, data: { kind: 'environment',
  environment: { key, source_asset_id: `source-${key}`, name: key }, plate: plate(key) } });
const prop = (key, kind = 'prop') => ({ id: `asset:${key}`, position: { x: 0, y: 0 },
  data: { kind: 'asset', asset: { key, name: key, kind }, plate: plate(key) } });
const seq = (key, y, extra = {}, shots = []) => ({ id: `seq:${key}`, position: { x: 0, y }, data: { kind: 'sequence',
  sequence: { key, label: key, title: key, character_keys: [], environment_key: '', ...extra }, shots } });
const vid = (key, refs = []) => ({ id: `vid:${key}`, position: { x: 0, y: 0 },
  data: { kind: 'video', sequenceKey: key, label: key, title: key, refs, prompt: 'Approved prompt', clipUrl: `https://video.test/${key}` } });
const ids = group => group.materials.map(m => m.id).sort();

test('legacy character sheets without wardrobe definitions still display their identity', () => {
  const hero = char('hero');
  delete hero.data.character.states;
  delete hero.data.states;
  const group = buildClipGroups([hero, seq('01', 0, { character_keys: ['hero'] }), vid('01')], [])[0];
  assert.equal(group.materials[0].previews[0].plate.referenceUrl, hero.data.identity.referenceUrl);
});

test('each clip groups only its inputs; shared materials keep the original node and stored board untouched', () => {
  const hero = char('hero');
  const nodes = [hero, char('extra'), env('hall'), env('garden'), prop('ring'),
    seq('02', 800, { character_keys: ['hero'], environment_key: 'garden' }), vid('02'),
    seq('01', 0, { character_keys: ['hero'], environment_key: 'hall', asset_keys: ['ring'] }), vid('01')];
  const before = JSON.stringify(nodes);
  const groups = buildClipGroups(nodes, []);
  assert.deepEqual(groups.map(g => g.key), ['01', '02']);
  assert.deepEqual(ids(groups[0]), ['asset:ring', 'char:hero', 'env:hall']);
  assert.deepEqual(ids(groups[1]), ['char:hero', 'env:garden']);
  for (const g of groups) {
    const m = g.materials.find(m => m.id === hero.id);
    assert.strictEqual(m.node, hero);
    assert.equal(m.usedBy, 2);
  }
  assert.equal(JSON.stringify(nodes), before);
  assert.strictEqual(groups[0].video.data, nodes.at(-1).data);
});

test('shot-level actors, second environment, crowd and recursively required props are included', () => {
  const nodes = [char('hero'), char('guard'), env('hall'), env('garden'), prop('crowd', 'background_group'), prop('box'), prop('letter'),
    seq('01', 0, { character_keys: ['hero'], environment_key: 'hall' }, [
      { character_keys: ['guard'], environment_key: 'garden', scene_present_asset_ids: ['crowd'], asset_presence: [{ asset_id: 'box', visibility: 'offscreen' }] },
    ]), vid('01')];
  const assets = [{ id: 'crowd', member_ids: ['source-guard'] }, { id: 'box', depends_on_asset_ids: ['letter'] },
    { id: 'letter', depends_on_asset_ids: ['box'] }];
  assert.deepEqual(ids(buildClipGroups(nodes, [], assets)[0]), ['asset:box', 'asset:crowd', 'asset:letter', 'char:guard', 'char:hero', 'env:garden', 'env:hall']);
});

test('all wardrobe sheets requested by shots are shown, independent of selected character tab', () => {
  const hero = char('hero');
  const day = seq('01', 0, {}, [{ character_keys: ['hero'], character_states: { hero: 'day' } }]);
  let groups = buildClipGroups([hero, day, vid('01')], []);
  assert.deepEqual(groups[0].materials[0].previews.map(p => p.plate.referenceUrl), ['https://assets.test/hero-day.png']);
  day.data.shots.push({ character_keys: ['hero'], character_states: { hero: 'night' } });
  groups = buildClipGroups([hero, day, vid('01')], []);
  assert.deepEqual(groups[0].materials[0].previews.map(p => p.key), ['day', 'night']);
});

test('missing wardrobe is visible as missing, never replaced by identity or another wardrobe', () => {
  const hero = char('hero'); delete hero.data.states.day;
  const groups = buildClipGroups([hero, seq('01', 0, { character_keys: ['hero'] }, [{ character_states: { hero: 'day' } }])], []);
  assert.equal(groups[0].materials[0].previews[0].plate.referenceUrl, undefined);
});

test('shot packages and legacy incoming edges are supported without traversing to unrelated clips', () => {
  const nodes = [char('hero'), prop('bag'), prop('unrelated'), seq('01', 0, { shot_package: { materials: { a: { asset_id: 'source-hero' } } } }), vid('01')];
  const edges = [{ source: 'asset:bag', target: 'vid:01' }, { source: 'asset:unrelated', target: 'vid:02' }];
  assert.deepEqual(ids(buildClipGroups(nodes, edges)[0]), ['asset:bag', 'char:hero']);
});

test('explicit atlas bindings do not include every character sharing its image URL', () => {
  const hero = char('hero'), extra = char('extra');
  extra.data.identity = hero.data.identity;
  const groups = buildClipGroups([hero, extra, seq('01', 0), vid('01', [{ label: '@image1', name: 'Hero',
    url: hero.data.identity.referenceUrl, kind: 'character', assetBindings: [{ assetId: 'source-hero' }] }])], []);
  assert.deepEqual(ids(groups[0]), ['char:hero']);
});

test('unresolved materials and imported storyboard references remain visible', () => {
  const nodes = [seq('01', 0, { character_keys: ['missing'], asset_keys: ['unbuilt'] }), vid('01', [
    { label: '@image1', name: 'Storyboard', kind: 'environment', url: 'https://assets.test/storyboard.png' },
  ])];
  const group = buildClipGroups(nodes, [], [{ id: 'unbuilt', kind: 'prop', name: 'Box' }])[0];
  assert.equal(group.materials.length, 3);
  assert.equal(group.materials.find(m => m.id === 'reference:unbuilt').name, 'Box');
  assert.equal(group.materials.find(m => m.kind === 'character').previews.length, 0);
  assert.equal(group.materials.find(m => m.kind === 'reference').previews[0].name, '@image1');
});

test('orphan videos and clip-owned keyframes are not dropped', () => {
  const video = vid('orphan'); video.data.startFrame = plate('start'); video.data.shotFrames = { s2: plate('s2') };
  const groups = buildClipGroups([video], []);
  assert.strictEqual(groups[0].video, video);
  assert.equal(groups[0].sequence, undefined);
  assert.deepEqual(groups[0].materials.map(m => m.kind), ['keyframe', 'keyframe']);
});

test('shared material updates appear in every clip without duplicating generation data', () => {
  const hero = char('hero');
  const other = [seq('01', 0, { character_keys: ['hero'] }), seq('02', 800, { character_keys: ['hero'] })];
  const updated = { ...hero, data: { ...hero.data, states: { ...hero.data.states, night: plate('new-night') } } };
  const groups = buildClipGroups([updated, ...other], []);
  assert.ok(groups.every(g => g.materials[0].previews[0].plate.referenceUrl === 'https://assets.test/new-night.png'));
  assert.ok(groups.every(g => g.materials[0].node.id === 'char:hero'));
});
