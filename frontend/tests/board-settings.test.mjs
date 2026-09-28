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
const fields = ["aspectRatio", "imageModel", "imageSize", "clipSeconds", "unmoderated", "kyc"];
const selectSettings = (value) => Object.fromEntries(fields.map((field) => [field, value[field]]));
const landscape = { aspectRatio: "16:9", imageModel: "gemini-3-pro-image", imageSize: "4K", clipSeconds: 30, unmoderated: false, kyc: true };
const portrait = { aspectRatio: "9:16", imageModel: "dola-seedream-5-0-pro", imageSize: "2K", clipSeconds: 20, unmoderated: true, kyc: false };
const nodes = [{ id: "script", type: "autoScript", position: { x: 0, y: 0 }, data: { kind: "script" } }];
const project = (id, settings) => ({ id, name: id, title: id, board: { ...settings, nodes, edges: [] } });
const caps = { image_models: ["gemini-3-pro-image", "dola-seedream-5-0-pro"], default_image_model: "gemini-3-pro-image" };
let boards;
let calls;
let capabilityHandler;
globalThis.fetch = async (path, init = {}) => {
  const body = init.body ? JSON.parse(init.body) : undefined;
  calls.push({ path, method: init.method, body });
  if (path === "/api/automation/capabilities") return { ok: true, json: async () => capabilityHandler ? capabilityHandler() : caps };
  if (path === "/api/automation/projects") return { ok: true, json: async () => [] };
  const id = path.split("/").at(-1);
  if (init.method === "PATCH") {
    boards[id] = { ...boards[id], ...body };
    return { ok: true, json: async () => ({ id }) };
  }
  assert.ok(boards[id], path);
  return { ok: true, json: async () => boards[id] };
};
const start = () => {
  boards = { portrait: project("portrait", portrait), landscape: project("landscape", landscape), legacy: project("legacy", {}) };
  calls = []; capabilityHandler = undefined;
  store.setState({ currentProjectId: null, capabilities: null, ...landscape });
};
const finish = () => store.setState({ currentProjectId: null });

test("each project loads and saves its own six generation settings", async () => {
  start();
  await store.getState().openProject("portrait");
  assert.deepEqual(selectSettings(store.getState()), portrait);
  await store.getState().saveNow();
  const saved = calls.find((call) => call.method === "PATCH");
  assert.deepEqual(selectSettings(saved.body.board), portrait);
  await store.getState().openProject("landscape");
  assert.deepEqual(selectSettings(store.getState()), landscape);
  await store.getState().openProject("portrait");
  assert.deepEqual(selectSettings(store.getState()), portrait);
  finish();
});

test("missing or invalid settings retain legacy defaults rather than poisoning the toolbar", async () => {
  start();
  await store.getState().openProject("legacy");
  assert.deepEqual(selectSettings(store.getState()), landscape);
  boards.invalid = project("invalid", { aspectRatio: "1:1", imageModel: "unknown", imageSize: "8K", clipSeconds: 999, unmoderated: "false", kyc: 1 });
  await store.getState().openProject("invalid");
  assert.deepEqual(selectSettings(store.getState()), landscape);
  finish();
});

test("a late capability response does not overwrite the opened project's saved model", async () => {
  start();
  let release;
  capabilityHandler = () => new Promise((resolve) => { release = resolve; });
  const loading = store.getState().loadCapabilities();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await store.getState().openProject("portrait");
  release(caps);
  await loading;
  assert.deepEqual(selectSettings(store.getState()), portrait);
  finish();
});

test("changing only a generation setting autosaves the project", async () => {
  start();
  await store.getState().openProject("portrait");
  calls.length = 0;
  store.getState().setAspectRatio("16:9");
  await new Promise((resolve) => setTimeout(resolve, 1300));
  const saved = calls.find((call) => call.method === "PATCH");
  assert.equal(saved.body.board.aspectRatio, "16:9");
  assert.equal(saved.body.board.imageModel, portrait.imageModel);
  finish();
});

test("file export and import carry project settings with the graph", async () => {
  start();
  await store.getState().openProject("portrait");
  const originalCreate = URL.createObjectURL;
  const originalRevoke = URL.revokeObjectURL;
  const originalDocument = globalThis.document;
  let exported;
  URL.createObjectURL = (blob) => { exported = blob; return "blob:test"; };
  URL.revokeObjectURL = () => {};
  globalThis.document = { createElement: () => ({ click() {} }) };
  try {
    store.getState().exportBoard();
    const text = await exported.text();
    assert.deepEqual(selectSettings(JSON.parse(text)), portrait);
    finish();
    store.setState(landscape);
    store.getState().importBoard(text);
    assert.deepEqual(selectSettings(store.getState()), portrait);
  } finally {
    URL.createObjectURL = originalCreate;
    URL.revokeObjectURL = originalRevoke;
    globalThis.document = originalDocument;
    finish();
  }
});
