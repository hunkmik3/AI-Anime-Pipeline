import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { build } from "esbuild";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

Object.defineProperty(globalThis, "localStorage", { configurable: true,
  value: { getItem: () => null, setItem() {}, removeItem() {} } });

async function load(path) {
  const bundled = await build({ entryPoints: [fileURLToPath(new URL(path, import.meta.url))],
    bundle: true, platform: "node", format: "cjs", write: false, logLevel: "silent",
    external: ["react", "react/jsx-runtime"] });
  const module = { exports: {} };
  new Function("require", "module", "exports", bundled.outputFiles[0].text)(
    createRequire(import.meta.url), module, module.exports);
  return module.exports;
}

const { SourceIssueSummary } = await load("../src/automation/SourceIssueSummary.tsx");
const { useAutomation: store } = await load("../src/store/automation.ts");
const render = (report) => renderToStaticMarkup(createElement(SourceIssueSummary, { report }));

function report() {
  return { status: "needs_review", method: "source_frames", digest: "context-summary",
    reviewed_shots: [1, 2, 3, 4], unresolved_shots: [2, 3], evidence: [],
    findings: [{ code: "identity_uncertain", message: "The rear view does not resolve identity.", shot: 2,
      category: "uncertainty", provenance: { finding_ids: ["earlier-1", "earlier-2"] } }],
    issue_summary: { input_findings: 10, active_issues: 4, duplicates_collapsed: 6,
      by_category: { technical: 1, visual: 1, uncertainty: 2 }, processed_shots: [1, 2, 3],
      retained_verified_shots: [4], corrected_shots: [1, 2, 3],
      corrected_and_verified_shots: [1], verified_shots: [1, 4], unresolved_shots: [2, 3],
      technical_actions: 1 } };
}

test("summary distinguishes corrected-and-verified shots from corrections still needing review", () => {
  const pending = report();
  const original = structuredClone(pending);
  const html = render(pending);
  assert.ok(html.includes("Đã sửa và xác nhận: 1 shot"));
  assert.ok(html.includes("Cần kiểm tra: 2 shot"));
  assert.ok(html.includes("4 mục đang mở"));
  assert.ok(html.includes("Chưa đủ bằng chứng: 2"));
  assert.ok(html.includes("Chưa đủ bằng chứng không đồng nghĩa với mô tả sai."));
  assert.ok(html.includes("Đã gộp 6 ghi chú lặp từ 10 ghi chú đầu vào."));
  assert.deepEqual(pending, original, "Displaying a report cannot resolve findings or accept it");
});

test("canonical unresolved shots cannot be displayed as corrected and verified", () => {
  const pending = report();
  pending.issue_summary.corrected_and_verified_shots = [1, 2];
  assert.ok(render(pending).includes("Đã sửa và xác nhận: 1 shot"));
});

test("human acceptance retains machine uncertainty without claiming automatic confirmation", () => {
  const accepted = { ...report(), status: "verified", unresolved_shots: [],
    review: { accepted_by: "reviewer", accepted_at: "2026-09-25T00:00:00Z",
      machine_status: "needs_review", accepted_shots: [2, 3] } };
  const html = render(accepted);
  assert.ok(html.includes("Đã sửa và xác nhận: 1 shot"));
  assert.ok(html.includes("4 mục đã được chấp nhận thủ công"));
  assert.ok(html.includes("đây không phải xác nhận tự động của AI"));
  assert.ok(!html.includes("mục đang mở"));
  assert.ok(html.includes("Chưa đủ bằng chứng: 2"));
});

test("legacy reports keep the existing panel fallback without fabricated summary counts", () => {
  const legacy = report();
  delete legacy.issue_summary;
  assert.equal(render(legacy), "");
});

test("board import and manual acceptance retain issue summary and original finding provenance", async (t) => {
  const pending = report();
  const board = { title: "Context trial", logline: "", runtime_seconds: 5, style: "cg3d",
    characters: [], environments: [], assets: [], production_assets: [], source_verification: pending,
    sequences: [{ key: "clip-01", label: "CLIP 01", title: "An observed room", duration_s: 5,
      character_keys: [], environment_key: "", summary: "", beat: "" }],
    shots: { "clip-01": [{ n: 1, duration_s: 5, action: ["A person turns away."], dialogue: [] }] } };
  t.mock.method(globalThis, "fetch", async (path) => {
    assert.equal(path, "/api/automation/videos/context-summary/board");
    return { ok: true, json: async () => board };
  });
  await store.getState().importReferenceBoard("context-summary");
  let saved = store.getState().promptContract("clip-01").source_verification;
  assert.deepEqual(saved.issue_summary, pending.issue_summary);
  assert.deepEqual(saved.findings[0].provenance, pending.findings[0].provenance);
  assert.equal(store.getState().adoptSourceVerification(pending), false);
  const accepted = { ...pending, status: "verified", unresolved_shots: [],
    findings: pending.findings.map((finding) => ({ ...finding, accepted: true })),
    review: { accepted_by: "reviewer", accepted_at: "2026-09-25T00:00:00Z",
      machine_status: "needs_review", accepted_shots: [2, 3] } };
  assert.equal(store.getState().adoptSourceVerification(accepted), true);
  saved = store.getState().promptContract("clip-01").source_verification;
  assert.deepEqual(saved.issue_summary, pending.issue_summary);
  assert.deepEqual(saved.findings[0].provenance, pending.findings[0].provenance);
  store.getState().reset();
});
