import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { build } from "esbuild";

const bundled = await build({ entryPoints: [fileURLToPath(new URL("../src/store/videoAnalysis.ts", import.meta.url))],
  bundle: true, platform: "node", format: "cjs", write: false, logLevel: "silent" });

function freshStore() {
  const module = { exports: {} };
  const timers = [];
  new Function("require", "module", "exports", "setTimeout", "clearTimeout", bundled.outputFiles[0].text)(
    createRequire(import.meta.url), module, module.exports,
    (callback) => { const timer = { callback, cancelled: false }; timers.push(timer); return timer; },
    (timer) => { if (timer) timer.cancelled = true; });
  return { store: module.exports.useVideoAnalysis, timers };
}

function detail(id = "sample-video") {
  return { id, name: "Video mẫu", status: "analysed", progress: {}, error: null,
    analysis: { shots: [{ shot: 1, start: 0, end: 3, source: { action: "Original action" }, dialogue: "Original speech" }],
      source_verification: { status: "needs_review", findings: [{ code: "source_mismatch", shot: 1, message: "Check this" }] } },
    cast: { characters: [{ key: "person", name: "Person" }] },
    adaptation: { shots: { "1": { action: ["Existing adaptation"] } } } };
}

const ok = (value) => ({ ok: true, json: async () => value });

test("refine keeps existing data, prevents duplicate submission, and polls until saved result", async (t) => {
  const { store, timers } = freshStore();
  const initial = detail();
  let current = { ...initial, status: "analysing", progress: { stage: "source_layers", done: 0, total: 2 } };
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  const requests = [];
  t.mock.method(globalThis, "fetch", async (path, init) => {
    requests.push({ path, method: init?.method ?? "GET" });
    if (path === "/api/automation/videos/sample-video/refine") return pending;
    if (path === "/api/automation/videos/sample-video") return ok(current);
    if (path === "/api/automation/videos") return ok([current]);
    throw new Error(`Unexpected request ${path}`);
  });
  store.setState({ openId: initial.id, detail: initial, videos: [initial], error: "Previous error" });
  const operation = store.getState().refine();
  await store.getState().refine();
  assert.equal(store.getState().refiningId, initial.id);
  assert.equal(store.getState().error, undefined);
  assert.equal(store.getState().detail, initial);
  assert.equal(requests.filter((request) => request.method === "POST").length, 1);
  release(ok({ status: "analysing" }));
  await operation;
  assert.equal(store.getState().refiningId, null);
  assert.equal(store.getState().detail.status, "analysing");
  assert.deepEqual(store.getState().detail.analysis, initial.analysis);
  assert.deepEqual(store.getState().detail.cast, initial.cast);
  assert.deepEqual(store.getState().detail.adaptation, initial.adaptation);
  assert.ok(timers.some((timer) => !timer.cancelled));

  current = { ...initial, analysis: { ...initial.analysis,
    shots: [{ ...initial.analysis.shots[0], source: { action: "Corrected action" } }] } };
  await timers.findLast((timer) => !timer.cancelled).callback();
  assert.equal(store.getState().detail.analysis.shots[0].source.action, "Corrected action");
  assert.equal(store.getState().detail.analysis.shots[0].dialogue, "Original speech");
  assert.equal(store.getState().detail.analysis.source_verification.status, "needs_review");
  assert.ok(!requests.some((request) => request.path.includes("/accept")));
  assert.ok(timers.every((timer) => timer.cancelled));
});

test("failed refinement preserves current data and displays the server error", async (t) => {
  const { store } = freshStore();
  const initial = detail();
  t.mock.method(globalThis, "fetch", async (path) => {
    if (path.endsWith("/refine")) return { ok: false, status: 409, statusText: "Conflict",
      json: async () => ({ detail: "Video đang được xử lý." }) };
    if (path === "/api/automation/videos/sample-video") return ok(initial);
    return ok([initial]);
  });
  store.setState({ openId: initial.id, detail: initial });
  await store.getState().refine();
  assert.deepEqual(store.getState().detail, initial);
  assert.equal(store.getState().error, "Video đang được xử lý.");
  assert.equal(store.getState().refiningId, null);
});

test("finishing a refine submission cannot replace a newly opened video's detail", async (t) => {
  const { store } = freshStore();
  const initial = detail();
  const other = detail("another-video");
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  t.mock.method(globalThis, "fetch", async (path) => {
    if (path.endsWith("/refine")) return pending;
    assert.equal(path, "/api/automation/videos");
    return ok([initial, other]);
  });
  store.setState({ openId: initial.id, detail: initial });
  const operation = store.getState().refine();
  store.setState({ openId: other.id, detail: other });
  release(ok({ status: "analysing" }));
  await operation;
  assert.equal(store.getState().openId, other.id);
  assert.equal(store.getState().detail, other);
});
