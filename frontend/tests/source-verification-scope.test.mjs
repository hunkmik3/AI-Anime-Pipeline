import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { build } from "esbuild";

Object.defineProperty(globalThis, "localStorage", { configurable: true,
  value: { getItem: () => null, setItem() {}, removeItem() {} } });
const bundled = await build({ entryPoints: [fileURLToPath(new URL("../src/store/automation.ts", import.meta.url))],
  bundle: true, platform: "node", format: "cjs", write: false, logLevel: "silent" });
const module = { exports: {} };
new Function("require", "module", "exports", bundled.outputFiles[0].text)(createRequire(import.meta.url), module, module.exports);
const store = module.exports.useAutomation;

test("scope limits survive board import and human acceptance without hiding visual findings", async () => {
  const scopeNotes = [
    { code: "audio_not_checked", message: "Audio was not independently checked." },
    { code: "continuous_motion_not_checked", message: "Intervening motion was not fully observed." },
  ];
  const finding = { code: "source_mismatch", message: "The box is open, not closed.", shot: 1 };
  const pending = { status: "needs_review", method: "source_frames", digest: "scope-test-source",
    scope: "sampled_source_frames; audio is not independently verified", scope_notes: scopeNotes,
    reviewed_shots: [], unresolved_shots: [1], findings: [finding], evidence: [] };
  const board = { title: "Scope trial", logline: "", runtime_seconds: 5, style: "cg3d",
    characters: [], environments: [], assets: [], production_assets: [], source_verification: pending,
    sequences: [{ key: "clip-01", label: "CLIP 01", title: "An empty room", duration_s: 5,
      character_keys: [], environment_key: "", summary: "", beat: "" }],
    shots: { "clip-01": [{ n: 1, duration_s: 5, action: ["An open box rests on the table."], dialogue: [] }] } };
  globalThis.fetch = async (path) => {
    assert.equal(path, "/api/automation/videos/scope-trial/board");
    return { ok: true, json: async () => board };
  };
  await store.getState().importReferenceBoard("scope-trial");
  const imported = store.getState().promptContract("clip-01").source_verification;
  assert.equal(imported.status, "needs_review");
  assert.deepEqual(imported.unresolved_shots, [1]);
  assert.deepEqual(imported.findings, [finding]);
  assert.deepEqual(imported.scope_notes, scopeNotes);
  assert.equal(store.getState().adoptSourceVerification(pending), false);

  const before = store.getState().currentFingerprint("clip-01");
  const accepted = { ...pending, status: "verified", reviewed_shots: [1], unresolved_shots: [],
    findings: [{ ...finding, accepted: true }], review: { accepted_by: "reviewer",
      accepted_at: "2026-09-25T00:00:00Z", machine_status: "needs_review", accepted_shots: [1] } };
  assert.equal(store.getState().adoptSourceVerification(accepted), true);
  const contract = store.getState().promptContract("clip-01").source_verification;
  assert.deepEqual(contract.scope_notes, scopeNotes);
  assert.deepEqual(contract.findings, [{ ...finding, accepted: true }]);
  assert.equal(contract.review.machine_status, "needs_review");
  assert.notEqual(store.getState().currentFingerprint("clip-01"), before);

  // Exported/imported JSON retains the limits and the manual-review provenance.
  const state = store.getState();
  state.importBoard(JSON.stringify({ nodes: state.nodes, edges: state.edges,
    sourceVerification: state.sourceVerification, productionAssets: state.productionAssets }));
  assert.deepEqual(store.getState().sourceVerification, accepted);
  store.getState().reset();
});
