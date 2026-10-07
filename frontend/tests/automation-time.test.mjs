import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { build } from 'esbuild';

process.env.TZ = 'Asia/Ho_Chi_Minh';
const bundled = await build({
  entryPoints: [fileURLToPath(new URL('../src/automation/projectTime.ts', import.meta.url))],
  bundle: true, platform: 'node', format: 'cjs', write: false, logLevel: 'silent',
});
const mod = { exports: {} };
new Function('require', 'module', 'exports', bundled.outputFiles[0].text)(createRequire(import.meta.url), mod, mod.exports);
const { projectTime } = mod.exports;
const now = Date.parse('2026-10-07T03:40:00Z');

for (const iso of ['2026-10-07T03:37:00.000000', '2026-10-07T03:37:00Z', '2026-10-07T10:37:00+07:00']) {
  test(`same instant displays three minutes ago, without a seven-hour offset: ${iso}`, () => {
    const result = projectTime(iso, now);
    assert.equal(result.label, '3 phút trước');
    assert.equal(result.dateTime, '2026-10-07T03:37:00.000Z');
    assert.match(result.title, /10:37:00/);
    assert.match(result.title, /07\/10\/2026/);
  });
}

test('rounds down without prematurely showing an hour or a day', () => {
  assert.equal(projectTime('2026-10-07T03:39:01Z', now).label, 'vừa xong');
  assert.equal(projectTime('2026-10-07T02:40:01Z', now).label, '59 phút trước');
  assert.equal(projectTime('2026-10-07T02:40:00Z', now).label, '1 giờ trước');
  assert.equal(projectTime('2026-10-06T03:40:01Z', now).label, '23 giờ trước');
});

test('older boards use Vietnamese local dates, including a UTC midnight boundary', () => {
  const result = projectTime('2026-10-05T23:30:00', now);
  assert.equal(result.label, '6/10/2026');
  assert.match(result.title, /06:30:00/);
  assert.match(result.title, /06\/10\/2026/);
});

test('clock skew cannot show negative elapsed time; invalid values do not show NaN', () => {
  assert.equal(projectTime('2026-10-07T03:40:10Z', now).label, 'vừa xong');
  for (const iso of ['', 'bad-date']) {
    assert.equal(projectTime(iso, now).label, 'Chưa rõ thời gian');
    assert.equal(projectTime(iso, now).dateTime, undefined);
  }
});

test('elapsed labels advance with the clock without changing the stored timestamp', () => {
  const iso = '2026-10-07T03:40:00Z';
  assert.equal(projectTime(iso, now).label, 'vừa xong');
  assert.equal(projectTime(iso, now + 60_000).label, '1 phút trước');
});

test('date display follows the viewer timezone, without a hardcoded seven-hour adjustment', () => {
  try {
    process.env.TZ = 'UTC';
    const result = projectTime('2026-10-05T23:30:00', now);
    assert.equal(result.label, '5/10/2026');
    assert.match(result.title, /23:30:00/);
    assert.equal(projectTime('2026-10-07T03:37:00', now).label, '3 phút trước');
  } finally {
    process.env.TZ = 'Asia/Ho_Chi_Minh';
  }
});
