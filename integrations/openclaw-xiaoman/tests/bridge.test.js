import test from 'node:test';
import assert from 'node:assert/strict';
import { createBridgeHandler } from '../index.js';

const config = { backendUrl: 'http://127.0.0.1:18083', tokenFile: 'token', bindingsFile: 'bindings' };
const ctx = { channelId: 'openclaw-weixin', accountId: 'test-bot', senderId: 'Alice', messageId: 'message-1' };
function setup(overrides = {}) {
  let calls = [];
  const handler = createBridgeHandler(config, {
    readFile: async p => p === 'token' ? 'a'.repeat(64) : JSON.stringify([{ account_id: 'test-bot', sender_id: 'Alice' }]),
    fetch: async (url, options) => {
      calls.push({ url, options });
      return Response.json({ reply: '我是小满。', action: 'chat' });
    }, ...overrides,
  });
  return { handler, calls };
}
test('non-Weixin messages are not claimed', async () => {
  const { handler, calls } = setup();
  assert.equal(await handler({ content: '你好' }, { channelId: 'mock' }), undefined);
  assert.equal(calls.length, 0);
});
test('returns Python reply, preserves trusted IDs and original text', async () => {
  const { handler, calls } = setup();
  assert.deepEqual(await handler({ content: '你好', body: 'untrusted envelope' }, ctx), { handled: true, text: '我是小满。' });
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    account_id: 'test-bot', sender_id: 'Alice', message_id: 'message-1', content: '你好',
  });
  assert.equal(calls[0].options.redirect, 'error');
});
test('unbound and case-mismatched users never reach Python', async () => {
  const { handler, calls } = setup();
  assert.equal((await handler({ content: '取消所有任务' }, { ...ctx, senderId: 'alice' })).handled, true);
  assert.equal(calls.length, 0);
});
test('Weixin 2.4.10 trusted private-chat peer supplies missing SenderId', async () => {
  const { handler, calls } = setup();
  assert.equal((await handler({ content: '你好' }, { ...ctx, senderId: undefined, conversationId: 'Alice' })).text, '我是小满。');
  assert.equal(JSON.parse(calls[0].options.body).sender_id, 'Alice');
});
test('unknown peer and conflicting sender/peer fail closed', async () => {
  const { handler, calls } = setup();
  assert.equal((await handler({ content: '查询待办' }, { ...ctx, senderId: undefined, conversationId: 'Bob' })).handled, true);
  assert.match((await handler({ content: '查询待办' }, { ...ctx, conversationId: 'Bob' })).text, /不一致/);
  assert.equal(calls.length, 0);
});
test('missing message ID fails closed, with no synthesized content-based identity', async () => {
  const { handler, calls } = setup();
  assert.equal((await handler({ content: '两分钟后提醒我喝水' }, { ...ctx, messageId: undefined })).handled, true);
  assert.equal(calls.length, 0);
});
test('backend error is claimed, never retried or delegated to default model', async () => {
  let requests = 0;
  const { handler } = setup({ fetch: async () => { requests++; return new Response(null, { status: 502 }); } });
  assert.match((await handler({ content: '提醒我喝水' }, ctx)).text, /查询待办/);
  assert.equal(requests, 1);
});
test('exception, media and invalid backend fail closed', async () => {
  const { handler, calls } = setup({ readFile: async () => { throw new Error('private details'); } });
  assert.equal((await handler({ content: '你好' }, ctx)).handled, true);
  assert.equal(calls.length, 0);
  const normal = setup();
  assert.match((await normal.handler({ content: '照片', media: [{ kind: 'image' }] }, ctx)).text, /只接入文字/);
  assert.equal(normal.calls.length, 0);
  assert.equal((await createBridgeHandler({ ...config, backendUrl: 'https://example.com' })({ content: '你好' }, ctx)).handled, true);
});
