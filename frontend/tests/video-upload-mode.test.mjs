import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { build } from 'esbuild';

Object.defineProperty(globalThis, 'localStorage', { configurable: true,
  value: { getItem: () => null, setItem() {}, removeItem() {} } });
const bundled = await build({ entryPoints: [fileURLToPath(new URL('../src/store/videoAnalysis.ts', import.meta.url))],
  bundle: true, platform: 'node', format: 'cjs', write: false, logLevel: 'silent' });
const module = { exports: {} };
new Function('require', 'module', 'exports', bundled.outputFiles[0].text)(createRequire(import.meta.url), module, module.exports);
const store = module.exports.useVideoAnalysis;

for (const mode of [undefined, 'standard', 'fast', 'one_pass']) {
  test(`new upload sends ${mode ?? 'one_pass by default'} without changing an explicit mode`, async () => {
    let submitted;
    globalThis.XMLHttpRequest = class {
      upload = {};
      status = 400;
      responseText = JSON.stringify({ detail: 'Stopped after request capture' });
      open(method, path) {
        assert.equal(method, 'POST');
        assert.equal(path, '/api/automation/videos');
      }
      send(form) {
        submitted = form;
        queueMicrotask(() => this.onload());
      }
    };
    const file = new Blob(['fixture'], { type: 'video/mp4' });
    file.name = 'source.mp4';
    const upload = mode === undefined ? store.getState().upload(file, null, {})
      : store.getState().upload(file, null, {}, 'standard', undefined, mode);
    await assert.rejects(upload, /Stopped after request capture/);
    assert.equal(submitted.get('analysis_mode'), mode ?? 'one_pass');
  });
}
