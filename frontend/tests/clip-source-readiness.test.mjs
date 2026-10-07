import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { build } from "esbuild";

Object.defineProperty(globalThis, "localStorage", { configurable: true,
  value: { getItem: () => null, setItem() {}, removeItem() {} } });
async function load(relative) {
  const bundled = await build({ entryPoints: [fileURLToPath(new URL(relative, import.meta.url))],
    bundle: true, platform: "node", format: "cjs", write: false, logLevel: "silent" });
  const module = { exports: {} };
  new Function("require", "module", "exports", bundled.outputFiles[0].text)(createRequire(import.meta.url), module, module.exports);
  return module.exports;
}
const { sourceReadyForShots } = await load("../src/automation/contracts.ts");
const { useAutomation: store } = await load("../src/store/automation.ts");
const report = () => ({ status: "needs_review", method: "source_frames", scope: "sampled_source_frames",
  digest: "full-film-digest", reviewed_shots: [1, 99], unresolved_shots: [99],
  findings: [{ shot: 99, code: "source_mismatch" }], scope_notes: [{ code: "audio_not_checked" }] });
const shot = { n: 1, source_shots: [1], duration_s: 5, action: ["The box stays closed."], dialogue: [], character_keys: [] };

test("one-pass readiness is structural and never requires a false verified claim", () => {
  const observed = { status: "observed", method: "one_pass_production", digest: "bound-inputs",
    structural_checks_passed: true, prepared_shots: [1], reviewed_shots: [], independent_review: false, findings: [] };
  assert.equal(sourceReadyForShots([shot], observed), true);
  for (const patch of [{ status: "verified" }, { structural_checks_passed: false }, { digest: "" },
    { prepared_shots: [] }, { findings: [{ shot: 1, code: "unresolved" }] }, { findings: [{ code: "global" }] }])
    assert.equal(sourceReadyForShots([shot], { ...observed, ...patch }), false);
  assert.equal(sourceReadyForShots([{ source_shots: [1, 2] }], observed), false);
  assert.equal(sourceReadyForShots([], observed), false);
  assert.equal(observed.status, "observed");
});

test("clip readiness leaves the film report unchanged and blocks selected or global issues", () => {
  const original = report();
  const before = structuredClone(original);
  assert.equal(sourceReadyForShots([shot], original), true);
  assert.deepEqual(original, before);
  for (const change of [
    { unresolved_shots: [1, 99] }, { reviewed_shots: [99] }, { scope: "" }, { digest: "" },
    { status: "unverified" }, { method: "text_only" },
    { findings: [{ shot: 1, code: "source_mismatch" }] },
    { findings: [{ code: "global_failure" }] }, { findings: [{ shot: 0, code: "invalid_scope" }] },
    { findings: [{ shot: 1, code: "mismatch", accepted: true }] },
  ]) assert.equal(sourceReadyForShots([shot], { ...report(), ...change }), false, JSON.stringify(change));
  assert.equal(sourceReadyForShots([{ source_shots: [1, 99] }], original), false);
  assert.equal(sourceReadyForShots([{}], original), false);
  assert.equal(sourceReadyForShots([], original), false);
});

test("generation sends full unchanged source provenance for a ready clip of a pending film", async () => {
  const source = report();
  const sequence = { key: "clip-01", label: "CLIP 01", title: "The closed box", duration_s: 5,
    character_keys: [], environment_key: "room", summary: "", beat: "" };
  const board = { title: "Partial-film test", style: "cg3d", aspect_ratio: "9:16", characters: [], assets: [],
    environments: [{ key: "room", source_asset_id: "room", name: "Room", summary: "", lighting: "", mood: "", lock: "",
      plate: { prompt: "Room", reference_url: "https://assets.invalid/room.png", media_id: "room-media" } }],
    production_assets: [{ id: "room", kind: "environment", name: "Room", description: "", reference_required: true }],
    source_verification: source, sequences: [sequence], shots: { "clip-01": [shot] } };
  const calls = [];
  globalThis.fetch = async (path, init = {}) => {
    const body = init.body ? JSON.parse(init.body) : undefined;
    calls.push({ path, body });
    let result;
    if (path.endsWith("/board")) result = board;
    else if (path.endsWith("/video/write")) result = { prompt: "Checked test prompt", duration_seconds: 5,
      writer: "mock-writer", coverage: { status: "verified" }, contract_digest: "real-server-required", coverage_token: "test-only-token" };
    else if (path.endsWith("/video/clip")) result = { url: "https://assets.invalid/clip.mp4", persisted: true, warnings: [] };
    else if (path.endsWith("/prompt")) result = { prompt: "Room prompt" };
    else if (path === "/api/automation/projects") result = [];
    else throw new Error(`Unexpected test API: ${path}`);
    return { ok: true, json: async () => result };
  };
  await store.getState().importReferenceBoard("scope-test");
  await store.getState().primeVideoPrompt("clip-01", { writer: true });
  await store.getState().generateClip("clip-01");
  const submitted = calls.find((call) => call.path.endsWith("/video/clip"));
  assert.ok(submitted);
  assert.deepEqual(submitted.body.prompt_contract.source_verification, source);
  assert.equal(store.getState().sourceVerification.status, "needs_review");
  assert.equal(store.getState().sourceVerification.review, undefined);
  assert.equal(store.getState().nodes.find((n) => n.id === "vid:clip-01").data.status, "done");
  store.getState().reset();
});

test("one atlas image transports distinct character, environment, crowd and prop bindings once", async () => {
  const plate = { prompt: "Four-part reference atlas", reference_url: "https://assets.invalid/atlas.png", media_id: "atlas-media" };
  const assets = ["person", "room", "group", "watch"].map((id, index) => ({ id,
    kind: ["character", "environment", "background_group", "prop"][index], name: id,
    description: `Distinct description for ${id}`, reference_required: true }));
  const board = { title: "Atlas trial", style: "cg3d", aspect_ratio: "9:16",
    characters: [{ key: "person", source_asset_id: "person", name: "Lead", role: "lead", summary: "Adult lead", identity_anchor: "",
      states: [{ key: "default", label: "Default", look: "", wardrobe: "", posture: "" }], plate: { ...plate, state_key: "default" } }],
    environments: [{ key: "room", source_asset_id: "room", name: "Room", summary: "Distinct room", lighting: "", mood: "", lock: "", plate }],
    assets: assets.slice(2).map((asset) => ({ ...asset, key: asset.id, plate,
      description: asset.id === "group" ? "Atlas upper-left cell: approved adult crowd." : undefined })),
    production_assets: assets, source_verification: report(),
    sequences: [{ key: "clip-01", label: "CLIP 01", title: "Atlas", duration_s: 5,
      character_keys: ["person"], environment_key: "room", asset_keys: ["group", "watch"], summary: "", beat: "" }],
    shots: { "clip-01": [{ ...shot, character_keys: ["person"],
      asset_presence: assets.map((asset) => ({ asset_id: asset.id, visibility: "visible" })) }] } };
  const calls = [];
  globalThis.fetch = async (path, init = {}) => {
    const body = init.body ? JSON.parse(init.body) : undefined;
    calls.push({ path, body });
    let result;
    if (path.endsWith("/board")) result = board;
    else if (path.endsWith("/video/write")) result = { prompt: "Checked atlas prompt", duration_seconds: 5,
      writer: "mock-writer", coverage: { status: "verified" }, contract_digest: "test-digest", coverage_token: "test-only-token" };
    else if (path.endsWith("/video/clip")) result = { url: "https://assets.invalid/atlas.mp4", persisted: true, warnings: [] };
    else if (path.endsWith("/prompt")) result = { prompt: "Atlas prompt" };
    else if (path === "/api/automation/projects") result = [];
    else throw new Error(`Unexpected test API: ${path}`);
    return { ok: true, json: async () => result };
  };
  await store.getState().importReferenceBoard("atlas-test");
  store.setState({ kyc: true });
  const collected = store.getState().collectVideoRefs("clip-01");
  assert.equal(collected.refs.length, 1);
  assert.deepEqual(collected.refs[0].assetBindings.map((binding) => binding.assetId), ["person", "room", "group", "watch"]);
  assert.equal(collected.characters[0].ref_label, "@image1");
  assert.equal(collected.environment.ref_label, "@image1");
  assert.deepEqual(collected.referenceAssets.map((asset) => asset.id), ["group", "watch"]);
  assert.ok(collected.referenceAssets.every((asset) => asset.ref_label === "@image1" && asset.description));
  assert.equal(collected.referenceAssets[0].description, "Atlas upper-left cell: approved adult crowd.");
  assert.equal(collected.referenceAssets[1].description, "Distinct description for watch");
  assert.equal(collected.refs[0].assetBindings.find((asset) => asset.assetId === "room").description, "Distinct room");
  assert.equal(store.getState().productionAssets.find((asset) => asset.id === "group").description, "Distinct description for group");
  await store.getState().primeVideoPrompt("clip-01", { writer: true });
  await store.getState().generateClip("clip-01");
  const submitted = calls.find((call) => call.path.endsWith("/video/clip"));
  assert.deepEqual(submitted.body.reference_urls, [plate.reference_url]);
  assert.deepEqual(submitted.body.kyc_media_ids, ["atlas-media"]);
  assert.equal(submitted.body.prompt_contract.reference_assets.length, 2);
  const watch = store.getState().nodes.find((node) => node.id === "asset:watch");
  store.getState().patchNode(watch.id, { plate: { ...watch.data.plate, mediaId: "conflicting-media" } });
  assert.throws(() => store.getState().collectVideoRefs("clip-01"), /Media ID khác nhau/);
  store.getState().patchNode(watch.id, { plate: watch.data.plate, asset: { ...watch.data.asset, id: "group" } });
  assert.throws(() => store.getState().collectVideoRefs("clip-01"), /reference lặp lại/);
  store.getState().reset();
});
