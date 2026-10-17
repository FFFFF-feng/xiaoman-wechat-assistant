import test from 'node:test';
import assert from 'node:assert/strict';
import { createReminderPump, createWelcomePump, createAnnouncementPump } from '../reminders.js';

const config = { backendUrl: 'http://127.0.0.1:18083', tokenFile: 'token', bindingsFile: 'bindings' };
function setup({ failure = false, rejectStart = false, unbound = false, emptyReceipt = false, failAck = false, welcome = false, announcement = false } = {}) {
  let claimed = false, sends = 0, result;
  const paths = [];
  const pump = (announcement ? createAnnouncementPump : welcome ? createWelcomePump : createReminderPump)(config, {
    readFile: async p => p === 'token' ? 'a'.repeat(64) : JSON.stringify([{ account_id: 'bot', sender_id: unbound ? 'Bob' : 'Alice' }]),
    hostConfig: {},
    loadAdapter: async () => ({ sendText: async ctx => {
      sends++;
      assert.equal(ctx.to, 'Alice');
      assert.equal(ctx.accountId, 'bot');
      if (failure) throw new Error('private provider details');
      return { messageId: emptyReceipt ? '' : 'platform-1' };
    } }),
    fetch: async (url, options) => {
      const path = new URL(url).pathname;
      paths.push(path);
      if (path.endsWith('/claim')) {
        const delivery = claimed ? null : { id: 1, account_id: 'bot', sender_id: 'Alice', claim_token: 'b'.repeat(64), text: '小满提醒：喝水' };
        claimed = true;
        return Response.json({ delivery });
      }
      if (path.endsWith('/start')) return Response.json({ send: true }, { status: rejectStart ? 409 : 200 });
      if (path.endsWith('/result')) {
        result = JSON.parse(options.body);
        return Response.json({ ok: true }, { status: failAck ? 500 : 200 });
      }
      throw new Error('unexpected path');
    },
  });
  return { pump, paths, sends: () => sends, result: () => result };
}
test('single native send, accepted ACK, no repeat after another poll', async () => {
  const t = setup();
  await t.pump.run(); await t.pump.run();
  assert.equal(t.sends(), 1);
  assert.equal(t.result().outcome, 'accepted');
  assert.equal(t.result().message_id, 'platform-1');
});
test('pre-send cancellation rejects claim, no platform request', async () => {
  const t = setup({ rejectStart: true }); await t.pump.run();
  assert.equal(t.sends(), 0); assert.equal(t.result(), undefined);
});
test('unknown binding never starts or sends', async () => {
  const t = setup({ unbound: true }); await t.pump.run();
  assert.equal(t.sends(), 0); assert.equal(t.paths.length, 1);
});
test('provider exception is unknown, not success or automatic resend', async () => {
  const t = setup({ failure: true }); await t.pump.run(); await t.pump.run();
  assert.equal(t.result().outcome, 'unknown'); assert.equal(t.sends(), 1);
});
test('empty native receipt is not platform acceptance', async () => {
  const t = setup({ emptyReceipt: true }); await t.pump.run();
  assert.equal(t.result().outcome, 'failed');
});
test('ACK failure after send does not cause a second physical send', async () => {
  const t = setup({ failAck: true }); await t.pump.run(); await t.pump.run();
  assert.equal(t.sends(), 1);
});
test('aborted service admits no new work', async () => {
  const t = setup(); const signal = AbortSignal.abort(); await t.pump.run(signal);
  assert.equal(t.paths.length, 0);
});

test('welcome uses separate authenticated outbox and exact account, sends once', async () => {
  const t = setup({ welcome: true }); await t.pump.run(); await t.pump.run();
  assert.equal(t.sends(), 1);
  assert.equal(t.result().outcome, 'accepted');
  assert.ok(t.paths.every(path => path.startsWith('/api/internal/clawbot/welcome/')));
});
test('uncertain welcome is not sent again after a poll', async () => {
  const t = setup({ welcome: true, failure: true }); await t.pump.run(); await t.pump.run();
  assert.equal(t.result().outcome, 'unknown'); assert.equal(t.sends(), 1);
});

test('announcement uses its own outbox, exact identity, accepted receipt once', async () => {
  const t=setup({announcement:true});await t.pump.run();await t.pump.run();
  assert.equal(t.sends(),1);assert.equal(t.result().outcome,'accepted');
  assert.ok(t.paths.every(p=>p.startsWith('/api/internal/clawbot/announcements/')));
});

test('uncertain update or lost ACK does not resend the same notice', async () => {
  for(const options of [{failure:true},{failAck:true},{emptyReceipt:true},{rejectStart:true}]) {
    const t=setup({announcement:true,...options});await t.pump.run();await t.pump.run();
    assert.equal(t.sends(),options.rejectStart?0:1);
    if(options.failure)assert.equal(t.result().outcome,'unknown');
    if(options.emptyReceipt)assert.equal(t.result().outcome,'failed');
  }
});
