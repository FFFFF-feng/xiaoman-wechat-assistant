import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../../../web/schedule.js', import.meta.url), 'utf8');
const start = source.indexOf('function snoozeTime(');
const end = source.indexOf('\nfunction taskButton(', start);
assert.ok(start >= 0 && end > start, 'snooze helper must be present in the actual frontend');
const snoozeTime = vm.runInNewContext(`(${source.slice(start, end)})`);
const now = Date.parse('2026-10-12T14:00:00+08:00');

test('future schedule snoozes from planned time, never moves backwards', () => {
  assert.equal(snoozeTime('2026-10-13T16:00:00', now), '2026-10-13T09:00:00.000Z');
});
test('overdue schedule snoozes from now', () => {
  assert.equal(snoozeTime('2026-10-12T13:00:00', now), '2026-10-12T07:00:00.000Z');
});
test('Shanghai naive time is independent of browser timezone and crosses midnight', () => {
  assert.equal(snoozeTime('2026-10-13T23:30:00', now), '2026-10-13T16:30:00.000Z');
});
test('explicit offset timestamp is not given a second timezone suffix', () => {
  assert.equal(snoozeTime('2026-10-13T08:00:00Z', now), '2026-10-13T09:00:00.000Z');
});

const retryStart = source.indexOf('async function requestWithRetry(');
const retryEnd = source.indexOf('\nasync function api(', retryStart);
assert.ok(retryStart >= 0 && retryEnd > retryStart);
const requestWithRetry = vm.runInNewContext(`(${source.slice(retryStart, retryEnd)})`, { AbortSignal });
const noWait = async () => {};
test('GET retries transient network errors at most three times', async () => {
  let calls = 0;
  await assert.rejects(requestWithRetry('/tasks', {}, async () => { calls++; throw new Error('offline'); }, noWait));
  assert.equal(calls, 3);
});
test('GET retries temporary edge errors but never authorization failure', async () => {
  let calls = 0;
  const ok = await requestWithRetry('/tasks', {}, async () => ({ status: ++calls === 1 ? 530 : 200 }), noWait);
  assert.equal(ok.status, 200);
  assert.equal(calls, 2);
  calls = 0;
  const denied = await requestWithRetry('/tasks', {}, async () => { calls++; return { status: 401 }; }, noWait);
  assert.equal(denied.status, 401);
  assert.equal(calls, 1);
});
test('POST is never automatically repeated after a network error or edge error', async () => {
  let calls = 0;
  await assert.rejects(requestWithRetry('/snooze', { method: 'POST' }, async () => { calls++; throw new Error('offline'); }, noWait),
    error => error.uncertain === true);
  assert.equal(calls, 1);
  calls = 0;
  const response = await requestWithRetry('/snooze', { method: 'POST' }, async () => { calls++; return { status: 530 }; }, noWait);
  assert.equal(response.status, 530);
  assert.equal(calls, 1);
});
