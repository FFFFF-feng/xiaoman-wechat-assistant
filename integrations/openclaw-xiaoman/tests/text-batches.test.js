import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync, existsSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createTextBatchQueue, createTextBatchPump } from '../text-batches.js';
import plugin, { createBridgeHandler } from '../index.js';

function fixture(t) {
  let time = 0;
  const q = createTextBatchQueue(':memory:', { now: () => time });
  t.after(() => q.close());
  const put = (content, n = 1, sender = 'Alice', account = 'bot') => q.enqueue({
    content, message_id: String(n), sender_id: sender, account_id: account });
  return { q, put, tick: n => { time += n; } };
}
const bindings = [{ account_id: 'bot', sender_id: 'Alice' }, { account_id: 'bot', sender_id: 'Bob' }];
const config = { backendUrl: 'http://127.0.0.1:18083', tokenFile: 'token', bindingsFile: 'bindings' };
const read = async name => name === 'token' ? 'a'.repeat(64) : JSON.stringify(bindings);

test('sequential native dispatch admits twenty fragments quickly and invokes Python once', async t => {
  const { q, tick } = fixture(t);
  const incoming = createBridgeHandler(config, { readFile: read, textQueue: q,
    fetch: async () => { throw new Error('must-not-block-admission'); } });
  for (let i = 0; i < 20; i++) {
    assert.deepEqual(await incoming({ content: `片段${i}` }, { channelId: 'openclaw-weixin',
      accountId: 'bot', senderId: 'Alice', messageId: `original-${i}` }), { handled: true });
    tick(1000);
  }
  assert.equal(q.claim(), null);
  tick(3000);
  const calls = [], sent = [];
  const direct = createBridgeHandler(config, { readFile: read, fetch: async (url, options) => {
    calls.push(JSON.parse(options.body)); return Response.json({ reply: '我听明白啦。' }); } });
  const pump = createTextBatchPump(config, { queue: q, handle: direct, readFile: read,
    loadAdapter: async () => ({ sendText: async item => { sent.push(item); return { messageId: 'accepted' }; } }) });
  await pump.run(); await pump.run();
  assert.equal(calls.length, 1); assert.equal(sent.length, 1);
  assert.equal(calls[0].content, Array.from({ length: 20 }, (_, i) => `片段${i}`).join('\n'));
  assert.equal(sent[0].to, 'Alice'); assert.equal(sent[0].accountId, 'bot');
  assert.deepEqual(q.counts().batches.map(v => ({ ...v })), [{ status: 'accepted', n: 1 }]);
});

test('quiet window resets; max wait bounds continuous input; cap stops unbounded batches', t => {
  const { q, put, tick } = fixture(t);
  put('提醒我'); tick(3000); put('明天十点写周报', 2); tick(3999);
  assert.equal(q.claim(), null); tick(1);
  const item = q.claim(); assert.equal(item.content, '提醒我\n明天十点写周报'); q.finish(item, 'accepted');
  for (let i = 0; i < 31; i++) { put('聊天', 100 + i); tick(1000); }
  const second = q.claim(); assert.equal(second.count, 31); q.finish(second, 'accepted');
  for (let i = 0; i < 40; i++) put('片段', 200 + i);
  assert.equal(q.claim().count, 40);
});

test('same-user batches serialize; different users/accounts are independent', t => {
  const { q, put, tick } = fixture(t);
  put('A'); tick(4000); const first = q.claim();
  put('A-next', 2); put('B', 1, 'Bob'); put('another-account', 1, 'Alice', 'bot-2'); tick(4000);
  const b = q.claim(), other = q.claim();
  assert.deepEqual(new Set([`${b.account_id}:${b.sender_id}`, `${other.account_id}:${other.sender_id}`]),
    new Set(['bot:Bob', 'bot-2:Alice']));
  assert.equal(q.claim(), null);
  q.finish(first, 'accepted'); assert.equal(q.claim().content, 'A-next');
});

test('all fragments retain order and negation; dedup never extends quiet time', t => {
  const { q, put, tick } = fixture(t);
  put('两分钟后提醒我喝水'); tick(1000); put('算了，不要提醒我', 2); tick(3500);
  put('算了，不要提醒我', 2); tick(500);
  const item = q.claim(); assert.equal(item.count, 2);
  assert.equal(item.content, '两分钟后提醒我喝水\n算了，不要提醒我');
  assert.throws(() => put('不同内容', 2), /conflict/);
  q.finish(item, 'accepted'); put('两分钟后提醒我喝水'); tick(10000); assert.equal(q.claim(), null);
});

test('pending survives restart; in-flight becomes unknown and is never auto-replayed', t => {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'xiaoman-batch-'));
  t.after(() => rmSync(dir, { recursive: true, force: true })); // Exact generated test-only directory.
  const file = path.join(dir, 'queue.sqlite'); let time = 0;
  let q = createTextBatchQueue(file, { now: () => time });
  assert.throws(() => createTextBatchQueue(file), /locked/);
  const item = { account_id: 'bot', sender_id: 'Alice', message_id: '1', content: '提醒我喝水' };
  q.enqueue(item); q.close(); time = 4000;
  q = createTextBatchQueue(file, { now: () => time });
  assert.equal(q.claim().content, item.content); q.close();
  q = createTextBatchQueue(file, { now: () => time });
  q.enqueue(item); assert.equal(q.claim(), null);
  assert.deepEqual(q.counts().batches.map(v => ({ ...v })), [{ status: 'unknown', n: 1 }]); q.close();
});

test('size boundaries retain unprocessed fragments without truncation', t => {
  const { q, put, tick } = fixture(t);
  put('a'.repeat(5000)); put('b'.repeat(4000), 2); tick(4000);
  const first = q.claim(); assert.equal(first.content.length, 5000);
  q.finish(first, 'accepted'); assert.equal(q.claim().content.length, 4000);
  assert.throws(() => put('c'.repeat(8001), 3), /invalid/);
});

test('revocation before work or after model reply prevents physical send', async t => {
  const { q, put, tick } = fixture(t); put('你好'); tick(4000);
  let allowed = true, sends = 0, modelCalls = 0;
  const pump = createTextBatchPump(config, { queue: q,
    readFile: async () => JSON.stringify(allowed ? bindings : []),
    handle: async () => { modelCalls++; allowed = false; return { handled: true, text: '回复' }; },
    loadAdapter: async () => ({ sendText: async () => { sends++; } }) });
  await pump.run(); assert.equal(modelCalls, 1); assert.equal(sends, 0);
  put('下一条', 2); tick(4000); await pump.run(); assert.equal(modelCalls, 1);
});

test('send timeout or empty receipt is unknown; no automatic retry', async t => {
  const { q, put, tick } = fixture(t); put('你好'); tick(4000); let sends = 0;
  const pump = createTextBatchPump(config, { queue: q, readFile: read,
    handle: async () => ({ handled: true, text: '回复' }),
    loadAdapter: async () => ({ sendText: async () => { sends++; throw new Error('private-token'); } }) });
  await pump.run(); await pump.run(); assert.equal(sends, 1);
  assert.equal(q.counts().batches[0].status, 'unknown');
});

test('queue admission refuses unbound/missing identity and respects pending voice barrier', async t => {
  const { q } = fixture(t);
  const handler = createBridgeHandler(config, { readFile: read, textQueue: q,
    readVoiceData: async () => 'fake-wav', fetch: async () => { throw new Error('must-not-call'); } });
  const ctx = { channelId: 'openclaw-weixin', accountId: 'bot', senderId: 'Alice', messageId: '1' };
  await handler({ content: '你好' }, { ...ctx, senderId: 'unbound' });
  await handler({ content: '你好' }, { ...ctx, messageId: '' }); assert.equal(q.counts().pending, 0);
  await handler({ content: '提醒我' }, ctx);
  const result = await handler({ content: '[语音]', media: [{ contentType: 'audio/wav' }] }, { ...ctx, messageId: '2' });
  assert.match(result.text, /前面的文字/); assert.equal(q.counts().pending, 1);
});

test('four users process concurrently, fifth waits; abort admits no work', async t => {
  const { q, put, tick } = fixture(t);
  const users = Array.from({ length: 5 }, (_, i) => `user-${i}`);
  for (const user of users) put('你好', 1, user);
  tick(4000);
  let calls = 0; const releases = [];
  const pump = createTextBatchPump(config, { queue: q,
    readFile: async () => JSON.stringify(users.map(sender_id => ({ account_id: 'bot', sender_id }))),
    handle: async () => { calls++; await new Promise(resolve => releases.push(resolve)); return { handled: true, text: '回复' }; },
    loadAdapter: async () => ({ sendText: async () => ({ messageId: 'accepted' }) }) });
  await pump.run(AbortSignal.abort()); assert.equal(calls, 0);
  const running = Array.from({ length: 4 }, () => pump.run());
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls, 4); await pump.run(); assert.equal(calls, 4);
  for (const release of releases) release(); await Promise.all(running);
  const last = pump.run(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls, 5); releases[4](); await last;
  assert.equal(q.counts().pending, 0);
});

test('backpressure rejects excess text without discarding admitted fragments', t => {
  const { q, put } = fixture(t);
  for (let i = 0; i < 120; i++) put('一件事', i);
  assert.throws(() => put('溢出消息', 121), /queue-full/);
  assert.equal(q.counts().pending, 120);
  put('一件事', 1); assert.equal(q.counts().pending, 120);
});

test('absent receipt is unknown and stop after inference avoids send', async t => {
  const { q, put, tick } = fixture(t); put('你好'); tick(4000);
  const controller = new AbortController(); let sends = 0;
  const pump = createTextBatchPump(config, { queue: q, readFile: read,
    handle: async () => { controller.abort(); return { handled: true, text: '回复' }; },
    loadAdapter: async () => ({ sendText: async () => { sends++; return {}; } }) });
  await pump.run(controller.signal); assert.equal(sends, 0); assert.equal(q.counts().batches[0].status, 'cancelled');
  put('下一条', 2); tick(4000);
  const next = createTextBatchPump(config, { queue: q, readFile: read,
    handle: async () => ({ handled: true, text: '回复' }),
    loadAdapter: async () => ({ sendText: async () => { sends++; return {}; } }) });
  await next.run(); await next.run(); assert.equal(sends, 1);
  assert.ok(q.counts().batches.some(v => v.status === 'unknown'));
});

test('enabled plugin registration connects silent admission to scheduled native delivery', async t => {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'xiaoman-plugin-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  // No real runtime, credentials, model or native channel is used.
  const localConfig = { ...config, mergeTextMessages: true,
    tokenFile: path.join(dir, 'bridge.key'), bindingsFile: path.join(dir, 'bindings.json') };
  writeFileSync(localConfig.tokenFile, 'a'.repeat(64));
  writeFileSync(localConfig.bindingsFile, JSON.stringify(bindings));
  t.mock.timers.enable({ apis: ['Date'], now: 0 });
  const hooks = new Map(), jobs = [], sent = []; let service;
  const logger = { info() {}, warn() {} };
  t.mock.method(globalThis, 'fetch', async () => Response.json({ reply: '本地统一回复' }));
  plugin.register({ pluginConfig: localConfig, logger,
    on: (name, fn) => hooks.set(name, fn), registerService: value => { service = value; },
    runtime: { channel: { outbound: { loadAdapter: async () => ({ sendText: async value => {
      sent.push(value); return { messageId: 'accepted' }; } }) } } } });
  assert.equal(existsSync(path.join(dir, 'text-batches.sqlite')), false, 'CLI registration must not open or mutate spool');
  const controller = new AbortController();
  service.start({ logger, config: {}, scheduler: { signal: controller.signal, schedule: job => jobs.push(job) } });
  assert.equal(jobs.filter(job => job.id.startsWith('text-batches-')).length, 4);
  const handler = hooks.get('before_dispatch');
  for (let i = 0; i < 3; i++) assert.deepEqual(await handler({ content: `内容${i}` }, {
    channelId: 'openclaw-weixin', accountId: 'bot', senderId: 'Alice', messageId: String(i) }), { handled: true });
  t.mock.timers.tick(4000);
  await jobs.find(job => job.id === 'text-batches-0').run();
  assert.equal(sent.length, 1); assert.equal(sent[0].text, '本地统一回复');
  controller.abort();
  await service.stop();
});
