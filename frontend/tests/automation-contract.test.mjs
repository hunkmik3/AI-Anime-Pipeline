import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { build } from "esbuild";

// Exercise the real store with fetch mocked. No provider/server is contacted.
Object.defineProperty(globalThis, "localStorage", { configurable: true,
  value: { getItem: () => null, setItem() {}, removeItem() {} } });
const bundled = await build({ entryPoints: [fileURLToPath(new URL("../src/store/automation.ts", import.meta.url))], bundle: true,
  platform: "node", format: "cjs", write: false, logLevel: "silent" });
const module = { exports: {} };
new Function("require", "module", "exports", bundled.outputFiles[0].text)(createRequire(import.meta.url), module, module.exports);
const store = module.exports.useAutomation;
const calls = [];
let handler;
globalThis.fetch = async (path, init = {}) => {
  const body = init.body ? JSON.parse(init.body) : undefined;
  calls.push({ path, body });
  const value = await handler(path, body);
  return { ok: true, json: async () => value };
};
const sheet = (key) => ({ prompt: `Sheet ${key}`, reference_url: `https://assets.invalid/${key}.png`,
  url: `https://assets.invalid/${key}.png`, media_id: `media-${key}` });
const verified = (scope) => ({ status: "verified", method: "source_frames", reviewed_shots: [1],
  unresolved_shots: [], findings: [], evidence: [], digest: `source-${scope}` });
function board(scope) {
  const ids = ["person", "set", "object", "group"].map((kind) => `${scope}-${kind}`);
  return { title: scope, logline: "", runtime_seconds: 5, style: "cg3d", aspect_ratio: "9:16",
    characters: [{ key: "person", source_asset_id: ids[0], name: `${scope} performer`, role: "lead", summary: "", identity_anchor: "",
      states: [{ key: "default", label: "Default", look: "", wardrobe: "", posture: "" }], plate: { ...sheet(ids[0]), state_key: "default" } }],
    environments: [{ key: "set", source_asset_id: ids[1], name: `${scope} set`, summary: "", lighting: "", mood: "", lock: "", plate: sheet(ids[1]) }],
    assets: [{ key: ids[2], kind: "prop", name: `${scope} object`, description: "A story object", plate: sheet(ids[2]) },
      { key: ids[3], kind: "background_group", name: `${scope} group`, description: "Background participants", plate: sheet(ids[3]) }],
    production_assets: ids.map((id, i) => ({ id, kind: ["character", "environment", "prop", "background_group"][i],
      name: id, description: "", reference_required: true, member_ids: [], depends_on_asset_ids: [] })),
    source_verification: verified(scope),
    sequences: [{ key: "clip-01", label: "CLIP 01", title: scope, duration_s: 5, summary: "", beat: "",
      environment_key: "set", character_keys: ["person"], asset_keys: ids.slice(2) }],
    shots: { "clip-01": [{ n: 1, source_shot: 1, source_shots: [1], duration_s: 5, framing: "WS", lens_mm: "35",
      action: ["The performer approaches."], dialogue: [], character_keys: ["person"],
      scene_present_asset_ids: ids, asset_presence: ids.map((asset_id) => ({ asset_id, visibility: "visible", state: "as seen" })) }] } };
}
function defaults(path) {
  if (path.endsWith("/prompt")) return { prompt: "Sheet prompt" };
  if (path === "/api/automation/projects") return [];
  throw new Error(`Unexpected mocked API: ${path}`);
}
async function importFilm(scope) {
  handler = async (path) => path.endsWith("/board") ? board(scope) : defaults(path);
  await store.getState().importReferenceBoard(scope);
}
const written = () => ({ prompt: "Verified prompt", duration_seconds: 5, end_state: "At the doorway", writer: "test-writer",
  warnings: [], coverage: { status: "verified", requirements: [], matches: [], semantic_review: { status: "verified", findings: [] } },
  contract_digest: "checked-digest", coverage_token: "signed-receipt" });
const video = () => store.getState().nodes.find((n) => n.id === "vid:clip-01").data;
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

test("asset import, coverage contract, and cross-film isolation", async (t) => {
  await t.test("freeform cinematic writing includes crowd and prop reference bindings", async () => {
    await importFilm("freeform");
    store.setState({ productionAssets: [], sourceVerification: undefined });
    const contract = store.getState().promptContract("clip-01");
    assert.equal(contract.reference_assets.length, 2);
    assert.deepEqual(contract.reference_assets.map((r) => r.kind).sort(), ["background_group", "prop"]);
    assert.equal(contract.production_assets, undefined);
  });
  await t.test("default prompt creation always calls the writer", async () => {
    await importFilm("default-writer");
    handler = async (path) => path.endsWith("/video/write") ? written() : defaults(path);
    calls.length = 0;
    await store.getState().primeVideoPrompt("clip-01");
    assert.equal(calls.filter((c) => c.path.endsWith("/video/write")).length, 1);
    assert.equal(calls.filter((c) => c.path.endsWith("/video/prompt")).length, 0);
    assert.equal(video().prompt, "Verified prompt");
  });
  await t.test("hydrates all asset kinds and retains expected metadata without images", async () => {
    await importFilm("workshop");
    const state = store.getState();
    assert.equal(state.style, "cg3d");
    assert.equal(state.nodes.filter((n) => n.data.kind === "asset").length, 2);
    assert.equal(state.collectVideoRefs("clip-01").refs.length, 4);
    assert.equal(state.collectVideoRefs("clip-01").referenceAssets.length, 2);
    assert.ok(state.collectVideoRefs("clip-01").referenceAssets.every((ref) => ref.id.startsWith("workshop-")));
    const char = state.nodes.find((n) => n.id === "char:person");
    state.patchNode(char.id, { identity: { prompt: "", status: "idle" }, states: { default: { prompt: "", status: "idle" } } });
    const refs = store.getState().collectVideoRefs("clip-01");
    assert.equal(refs.characters.length, 1);
    assert.equal(refs.refs.length, 3);
    assert.equal(refs.referenceAssets.length, 2);
    assert.equal(refs.characters[0].ref_label, undefined);
  });

  await t.test("sends receipt with exact nested contract and rewrites after a shot changes", async () => {
    await importFilm("desert");
    handler = async (path) => path.endsWith("/video/write") ? written()
      : path.endsWith("/video/clip") ? { url: "https://assets.invalid/clip.mp4", persisted: true, warnings: [] } : defaults(path);
    await store.getState().primeVideoPrompt("clip-01", { writer: true });
    assert.equal(store.getState().promptContract("clip-01").sequence.preserve_source_shots, store.getState().preserveSourceShots);
    calls.length = 0;
    await store.getState().generateClip("clip-01");
    assert.equal(calls.filter((c) => c.path.endsWith("/video/write")).length, 0);
    const clip = calls.find((c) => c.path.endsWith("/video/clip"));
    assert.equal(clip.body.coverage_token, "signed-receipt");
    assert.equal(clip.body.prompt_contract.source_verification.digest, "source-desert");
    const bound = [...clip.body.prompt_contract.characters, clip.body.prompt_contract.environment, ...clip.body.prompt_contract.reference_assets]
      .filter((ref) => ref?.ref_label).sort((a, b) => Number(a.ref_label.slice(6)) - Number(b.ref_label.slice(6)));
    assert.equal(new Set(bound.map((ref) => ref.ref_label)).size, 4);
    assert.deepEqual(clip.body.reference_urls, bound.map((ref) => ref.ref_url));
    assert.ok(bound.every((ref) => ref.media_id));
    const seq = store.getState().nodes.find((n) => n.id === "seq:clip-01");
    store.getState().patchNode(seq.id, { shots: [{ ...seq.data.shots[0], action: ["The performer leaves."] }] });
    calls.length = 0;
    await store.getState().generateClip("clip-01");
    assert.equal(calls.filter((c) => c.path.endsWith("/video/write")).length, 1);
    const asset = store.getState().nodes.find((n) => n.id === "asset:desert-object");
    store.getState().patchNode(asset.id, { plate: { ...asset.data.plate, referenceUrl: "https://assets.invalid/revised-object.png" } });
    calls.length = 0;
    await store.getState().generateClip("clip-01");
    assert.equal(calls.filter((c) => c.path.endsWith("/video/write")).length, 1);
  });

  await t.test("new imports, legacy boards, reset, and premise breakdown do not keep another film's registry", async () => {
    await importFilm("harbor");
    assert.equal(store.getState().sourceVerification.digest, "source-harbor");
    assert.ok(store.getState().productionAssets.every((asset) => asset.id.startsWith("harbor-")));
    store.getState().importBoard(JSON.stringify({ nodes: [{ id: "script", type: "autoScript", position: { x: 0, y: 0 }, data: { kind: "script" } }] }));
    assert.deepEqual(store.getState().productionAssets, []);
    assert.equal(store.getState().sourceVerification, undefined);
    await importFilm("observatory");
    handler = async (path) => path === "/api/automation/projects/legacy" ? { id: "legacy", title: "Legacy", board: {} } : defaults(path);
    await store.getState().openProject("legacy");
    assert.deepEqual(store.getState().productionAssets, []);
    assert.equal(store.getState().sourceVerification, undefined);
    store.setState({ currentProjectId: null });
    await importFilm("garden");
    store.getState().reset();
    assert.deepEqual(store.getState().productionAssets, []);
    assert.equal(store.getState().sourceVerification, undefined);
    await importFilm("station");
    handler = async (path) => path.endsWith("/breakdown") ? { ...board("premise"), assets: [] } : defaults(path);
    store.getState().setScript("An original story.");
    await store.getState().runBreakdown();
    assert.deepEqual(store.getState().productionAssets, []);
    assert.equal(store.getState().sourceVerification, undefined);
  });

  await t.test("strict KYC transports both verified URL order and matching media IDs", async () => {
    await importFilm("studio");
    store.setState({ kyc: true });
    handler = async (path) => path.endsWith("/video/write") ? written()
      : path.endsWith("/video/clip") ? { url: "https://assets.invalid/clip.mp4", persisted: true, warnings: [] } : defaults(path);
    calls.length = 0;
    await store.getState().generateClip("clip-01");
    const clip = calls.find((call) => call.path.endsWith("/video/clip"));
    assert.equal(clip.body.reference_urls.length, 4);
    assert.equal(clip.body.kyc_media_ids.length, 4);
    const contract = clip.body.prompt_contract;
    const bound = [...contract.characters, contract.environment, ...contract.reference_assets]
      .filter((ref) => ref?.ref_label).sort((a, b) => Number(a.ref_label.slice(6)) - Number(b.ref_label.slice(6)));
    assert.deepEqual(clip.body.kyc_media_ids, bound.map((ref) => ref.media_id));
    store.setState({ kyc: false });
  });

  await t.test("late writer response cannot overwrite another film with the same clip key", async () => {
    await importFilm("old-film");
    let resolve;
    handler = async (path) => path.endsWith("/video/write") ? new Promise((done) => { resolve = done; }) : defaults(path);
    const writing = store.getState().primeVideoPrompt("clip-01", { writer: true });
    await tick();
    await importFilm("new-film");
    resolve(written());
    await writing;
    assert.equal(video().prompt, "");
    assert.equal(store.getState().sourceVerification.digest, "source-new-film");
  });

  await t.test("late image result cannot overwrite another film's character plate", async () => {
    await importFilm("first-film");
    let resolve;
    handler = async (path) => path === "/api/automation/plate" ? new Promise((done) => { resolve = done; }) : defaults(path);
    const generation = store.getState().generate("char:person", "identity");
    await tick();
    await importFilm("second-film");
    resolve({ images: [{ url: "https://assets.invalid/old-film.png", reference_url: "https://assets.invalid/old-film.png" }] });
    await generation;
    const character = store.getState().nodes.find((n) => n.id === "char:person");
    assert.equal(character.data.identity.referenceUrl, "https://assets.invalid/second-film-person.png");
  });

  await t.test("asset image dependencies keep ordered bindings, reject missing plates, and preserve duplicate URLs", async () => {
    await importFilm("archive");
    const state = store.getState();
    store.setState({ productionAssets: state.productionAssets.map((asset) => asset.id === "archive-object"
      ? { ...asset, depends_on_asset_ids: ["archive-person", "archive-set"], member_ids: ["archive-person"] } : asset) });
    const person = state.nodes.find((n) => n.id === "char:person");
    const env = state.nodes.find((n) => n.id === "env:set");
    const asset = state.nodes.find((n) => n.id === "asset:archive-object");
    state.patchNode(env.id, { plate: { ...env.data.plate, referenceUrl: person.data.identity.referenceUrl } });
    state.patchNode(asset.id, { plate: { ...asset.data.plate, prompt: "" } });
    calls.length = 0;
    await state.primePrompts();
    const prompt = calls.find((call) => call.path.endsWith("/prompt") && call.body.kind === "prop");
    assert.deepEqual(prompt.body.dependency_references.map((ref) => ref.id), ["archive-person", "archive-set"]);
    assert.deepEqual(prompt.body.dependency_references.map((ref) => ref.ref_label), ["@image1", "@image2"]);
    state.patchNode(person.id, { identity: { prompt: "", status: "idle" }, states: { default: { prompt: "", status: "idle" } } });
    calls.length = 0;
    await state.generate(asset.id, "plate");
    assert.equal(calls.length, 0);
    assert.match(store.getState().nodes.find((n) => n.id === asset.id).data.plate.error, /thành phần/);
    state.patchNode(person.id, { identity: person.data.identity, states: person.data.states });
    handler = async (path) => path === "/api/automation/plate" ? { images: [sheet("generated-object")] } : defaults(path);
    await state.generate(asset.id, "plate");
    const request = calls.find((call) => call.path === "/api/automation/plate");
    assert.deepEqual(request.body.reference_urls, [person.data.identity.referenceUrl, person.data.identity.referenceUrl]);
  });

  await t.test("unverified source and unsupported transport never submit a generated clip", async () => {
    await importFilm("museum");
    handler = async (path) => path.endsWith("/video/write") ? written() : defaults(path);
    store.setState({ sourceVerification: { ...verified("museum"), status: "needs_review" } });
    calls.length = 0;
    await store.getState().generateClip("clip-01");
    assert.equal(calls.some((c) => c.path.endsWith("/video/clip")), false);
    assert.match(video().error, /Agent 1/);
    store.setState({ sourceVerification: verified("museum") });
    store.getState().patchNode("vid:clip-01", { chainFromPrevious: true });
    await store.getState().generateClip("clip-01");
    assert.match(video().error, /reference/);
  });
  store.getState().reset();
});

// Boards made before the source agents: no inventory, or only a legacy report.
function legacyBoard(scope, sourceVerification) {
  const b = board(scope);
  delete b.production_assets;
  delete b.assets;
  delete b.source_verification;
  if (sourceVerification) b.source_verification = sourceVerification;
  b.sequences[0].asset_keys = [];
  b.shots["clip-01"][0].asset_presence = [];
  b.shots["clip-01"][0].scene_present_asset_ids = [];
  return b;
}
const clipDone = { url: "https://assets.invalid/clip.mp4", persisted: true, warnings: [] };

test("boards from before the source agents keep working", async (t) => {
  const { contractFingerprint, sameFingerprint, isStrictBoard } = await (async () => {
    const out = await build({ entryPoints: [fileURLToPath(new URL("../src/automation/contracts.ts", import.meta.url))],
      bundle: true, platform: "node", format: "cjs", write: false, logLevel: "silent" });
    const m = { exports: {} };
    new Function("require", "module", "exports", out.outputFiles[0].text)(createRequire(import.meta.url), m, m.exports);
    return m.exports;
  })();

  await t.test("fingerprints are short hashes and still match those stored as JSON", () => {
    const contract = { b: [1, 2], a: { y: 1, x: 2 } };
    const hashed = contractFingerprint(contract);
    assert.match(hashed, /^h1:[0-9a-f]{16}$/);
    assert.equal(contractFingerprint({ a: { x: 2, y: 1 }, b: [1, 2] }), hashed);
    assert.ok(sameFingerprint(JSON.stringify({ a: { x: 2, y: 1 }, b: [1, 2] }), hashed));
    assert.ok(!sameFingerprint(undefined, hashed));
    assert.ok(!isStrictBoard([], { status: "unverified", findings: [{ code: "legacy_analysis" }] }));
    assert.ok(isStrictBoard([], { status: "unverified" }));
  });

  for (const [label, report] of [["no inventory", undefined],
    ["a legacy report", { status: "unverified", findings: [{ code: "legacy_analysis", message: "" }] }]]) {
    await t.test(`${label}: Gen keeps a written prompt that has no fingerprint`, async () => {
      handler = async (path) => path.endsWith("/board") ? legacyBoard("old", report) : defaults(path);
      await store.getState().importReferenceBoard("old");
      const { refs } = store.getState().collectVideoRefs("clip-01");
      store.getState().patchNode("vid:clip-01", { prompt: "Approved prompt", promptBy: "gpt-6-astra", refs,
        inputFingerprint: undefined });
      handler = async (path) => path.endsWith("/video/clip") ? clipDone : defaults(path);
      calls.length = 0;
      await store.getState().generateClip("clip-01");
      assert.equal(calls.filter((c) => c.path.endsWith("/video/write")).length, 0);
      const clip = calls.find((c) => c.path.endsWith("/video/clip"));
      assert.equal(clip.body.prompt, "Approved prompt");
      assert.equal(clip.body.prompt_contract, undefined);
      assert.equal(clip.body.sequence_key, "clip-01");
      assert.equal(video().status, "done");
    });
  }

  await t.test("a prompt placed by hand is generated as it is", async () => {
    handler = async (path) => path.endsWith("/board") ? legacyBoard("hand") : defaults(path);
    await store.getState().importReferenceBoard("hand");
    const { refs } = store.getState().collectVideoRefs("clip-01");
    store.getState().patchNode("vid:clip-01", { prompt: "My own prompt", promptBy: "manual", refs });
    handler = async (path) => path.endsWith("/video/clip") ? clipDone : defaults(path);
    calls.length = 0;
    await store.getState().generateClip("clip-01");
    assert.equal(calls.find((c) => c.path.endsWith("/video/clip")).body.prompt, "My own prompt");
  });

  await t.test("a strict board still rewrites a prompt that predates its contract", async () => {
    await importFilm("strict");
    const { refs } = store.getState().collectVideoRefs("clip-01");
    store.getState().patchNode("vid:clip-01", { prompt: "Old prompt", promptBy: "gpt-6-astra", refs,
      inputFingerprint: undefined });
    handler = async (path) => path.endsWith("/video/write") ? written() : path.endsWith("/video/clip") ? clipDone : defaults(path);
    calls.length = 0;
    await store.getState().generateClip("clip-01");
    assert.equal(calls.filter((c) => c.path.endsWith("/video/write")).length, 1);
    assert.equal(calls.find((c) => c.path.endsWith("/video/clip")).body.prompt, "Verified prompt");
  });

  await t.test("the fingerprint is not rebuilt while nothing it depends on changes", () => {
    const state = store.getState();
    const first = state.currentFingerprint("clip-01");
    store.setState({ nodes: state.nodes.map((n) => ({ ...n, position: { x: n.position.x + 5, y: n.position.y } })) });
    assert.equal(store.getState().currentFingerprint("clip-01"), first);
    const seq = store.getState().nodes.find((n) => n.id === "seq:clip-01");
    store.getState().patchNode(seq.id, { shots: [{ ...seq.data.shots[0], action: ["Changed."] }] });
    assert.notEqual(store.getState().currentFingerprint("clip-01"), first);
  });
  store.getState().reset();
});

test("accepting the review reaches a board cut from the same inventory", async () => {
  const pending = { ...verified("review"), status: "needs_review", unresolved_shots: [1] };
  handler = async (path) => path.endsWith("/board") ? { ...board("review"), source_verification: pending } : defaults(path);
  await store.getState().importReferenceBoard("review");
  const before = store.getState().currentFingerprint("clip-01");
  const accepted = { ...verified("review"), review: { accepted_by: "local user", accepted_at: "2026-09-25T00:00:00Z" } };
  // Another inventory, or a report nobody accepted, leaves the board alone.
  assert.equal(store.getState().adoptSourceVerification({ ...accepted, digest: "source-other" }), false);
  assert.equal(store.getState().adoptSourceVerification(pending), false);
  assert.equal(store.getState().sourceVerification.status, "needs_review");
  assert.equal(store.getState().adoptSourceVerification(accepted), true);
  assert.equal(store.getState().sourceVerification.review.accepted_by, "local user");
  // The contract changed, so a prompt written against the old verdict is stale.
  assert.notEqual(store.getState().currentFingerprint("clip-01"), before);
  store.getState().reset();
  assert.equal(store.getState().adoptSourceVerification(accepted), false);
});

for (const previousPrompt of ["", "A manually approved prompt."]) {
  test(`writer failure keeps refs visible and preserves ${previousPrompt ? "the existing manual prompt" : "an empty draft"}`, async () => {
    await importFilm("writer-failure");
    const state = store.getState();
    state.patchNode("vid:clip-01", { prompt: previousPrompt, promptBy: previousPrompt ? "manual" : undefined,
      refs: [], coverageToken: "previous-token", contractDigest: "previous-digest" });
    let rejectWriter;
    handler = async (path) => path.endsWith("/video/write")
      ? new Promise((_resolve, reject) => { rejectWriter = reject; }) : defaults(path);
    const writing = store.getState().primeVideoPrompt("clip-01", { writer: true });
    await tick();
    assert.equal(video().refs.length, 4, "available sheets appear while the writer is pending");
    assert.equal(video().prompt, previousPrompt);
    const prop = store.getState().nodes.find((node) => node.id === "asset:writer-failure-object");
    state.patchNode(prop.id, { plate: { ...prop.data.plate, referenceUrl: "https://assets.invalid/new-prop.png" } });
    rejectWriter(new Error("Writer returned no usable content."));
    await assert.rejects(writing, /no usable content/);
    assert.equal(video().refs.length, 4);
    assert.ok(video().refs.some((ref) => ref.url === "https://assets.invalid/new-prop.png"));
    assert.equal(video().prompt, previousPrompt);
    assert.equal(video().promptBy, previousPrompt ? "manual" : undefined);
    assert.equal(video().coverageToken, "previous-token");
    assert.equal(video().contractDigest, "previous-digest");
    assert.match(video().error, /no usable content/);
  });
}

test('manual video edits invalidate receipts and are verified unchanged, never rewritten',async()=>{
 await importFilm('manual');
 store.setState({currentProjectId:null});
 store.getState().editVideoPrompt('clip-01','User edited exact prompt');
 assert.equal(video().promptBy,'manual');assert.equal(video().coverageToken,undefined);
 calls.length=0;
 handler=async(path,body)=>path.endsWith('/verify-prompt')?{...written(),prompt:body.prompt}
  :path.endsWith('/video/clip')?{url:'https://test/manual-video',persisted:true}:defaults(path);
 await store.getState().generateClip('clip-01');
 assert.equal(calls.filter(c=>c.path.endsWith('/verify-prompt')).length,1);
 assert.equal(calls.filter(c=>c.path.endsWith('/video/write')).length,0);
 assert.equal(calls.find(c=>c.path.endsWith('/video/clip')).body.prompt,'User edited exact prompt');
 assert.equal(video().prompt,'User edited exact prompt');assert.equal(video().promptBy,'manual');
});

test('failed manual verification cannot submit a clip or overwrite the draft',async()=>{
 await importFilm('manual-fail');
 store.getState().editVideoPrompt('clip-01','My incomplete prompt');calls.length=0;
 handler=async(path)=>{if(path.endsWith('/verify-prompt'))throw new Error('Missing dialogue');return defaults(path)};
 await store.getState().generateClip('clip-01');
 assert.equal(video().prompt,'My incomplete prompt');assert.match(video().error,/Missing dialogue/);
 assert.equal(calls.some(c=>c.path.endsWith('/video/write')||c.path.endsWith('/video/clip')),false);
});

test('editing during manual verification preserves the newest draft without stale receipt',async()=>{
 await importFilm('manual-race');store.getState().editVideoPrompt('clip-01','First draft');
 let finish;handler=async(path)=>path.endsWith('/verify-prompt')?new Promise(resolve=>{finish=resolve}):defaults(path);
 const pending=store.getState().verifyVideoPrompt('clip-01');await tick();
 store.getState().editVideoPrompt('clip-01','Newer draft');finish({...written(),prompt:'First draft'});
 await assert.rejects(()=>pending,/đã thay đổi/);
 assert.equal(video().prompt,'Newer draft');assert.equal(video().coverageToken,undefined);
});
