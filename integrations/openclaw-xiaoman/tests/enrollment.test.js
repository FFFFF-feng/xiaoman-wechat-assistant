import {test} from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {createLoginParser,confirmedIdentity,registrySnapshot,isolatedEnvironment,runEnrollment} from '../enrollment.js';

test('split UTF8 QR and prompt parsing, refresh and no secret in events other than QR', () => {
  const parser = createLoginParser();
  const input = Buffer.from('https://liteapp.weixin.qq.com/test?qrcode=test-one\n输入手机微信显示的数字，以继续连接：');
  const events = [...parser.push(input.subarray(0,61)),...parser.push(input.subarray(61))];
  assert.deepEqual(events.map(v => v.type), ['qr','needs_code']);
  assert.equal(parser.push(Buffer.from(' ')).length,0);
  assert.equal(parser.push(Buffer.from('https://liteapp.weixin.qq.com/test?qrcode=refreshed\n'))[0].type,'qr');
  parser.push(Buffer.from('已将此 OpenClaw 连接到微信。')); assert.equal(parser.success,true);
});
test('untrusted QR host ignored', () => {
  const parser = createLoginParser();
  assert.deepEqual(parser.push(Buffer.from('https://evil.example/qrcode=x\n')),[]);
});
test('confirmed registry delta exactly one, existing scanner not reassigned', () => {
  const old = {account_id:'a',sender_id:'Alice',fingerprint:'old'};
  const before = new Map([['a',old]]);
  const next = {account_id:'b',sender_id:'Bob',fingerprint:'new'};
  assert.deepEqual(confirmedIdentity(before,new Map([['a',old],['b',next]]),true),{account_id:'b',sender_id:'Bob'});
  assert.throws(() => confirmedIdentity(before,before,true));
  assert.throws(() => confirmedIdentity(before,new Map([['b',{...next,sender_id:'Alice'}]]),true));
  assert.throws(() => confirmedIdentity(before,new Map([['b',next]]),false));
});
test('native account path traversal and incomplete identity rejected', async () => {
  await assert.rejects(registrySnapshot('/private',async () => JSON.stringify(['../token'])));
  await assert.rejects(registrySnapshot('/private',async p => p.endsWith('accounts.json') ? '["a"]' : '{}'));
});
test('fresh installation has an empty registry; corrupt, denied and missing account files fail closed', async () => {
  const missing = Object.assign(new Error('fixture missing'), {code:'ENOENT'});
  const empty = await registrySnapshot('/private', async () => {throw missing;});
  assert.equal(empty.size, 0);
  await assert.rejects(registrySnapshot('/private', async () => '{invalid-json'));
  await assert.rejects(registrySnapshot('/private', async () => {throw Object.assign(new Error('fixture denied'), {code:'EACCES'});}));
  await assert.rejects(registrySnapshot('/private', async p => {
    if (p.endsWith('accounts.json')) return '["a"]';
    throw missing;
  }));
});
test('CLI child cannot inherit other instance or model credentials', () => {
  const env = isolatedEnvironment({OPENCLAW_STATE_DIR:'wrong',MODEL_API_KEY:'secret',GH_TOKEN:'secret',PATH:'safe'},'/root','/root/state');
  assert.equal(env.MODEL_API_KEY,undefined); assert.equal(env.GH_TOKEN,undefined);
  assert.equal(env.OPENCLAW_STATE_DIR,'/root/state'); assert.equal(env.PATH,'safe');
});
function fakeChild() {
  const child = new EventEmitter(); child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  child.stdin = {write:code => {child.code = code;}}; child.kill = () => {child.killed=true;}; return child;
}
test('official CLI confirmed result completes once and QR never goes in completion', async () => {
  const child = fakeChild(), calls = []; let snapshotCount=0, pauses=0;
  const entry = {account_id:'b',sender_id:'Bob',fingerprint:'new'};
  await runEnrollment({id:1,lease:'a'.repeat(64)}, {
    snapshot:async () => snapshotCount++ ? new Map([['b',entry]]) : new Map(),
    call:async (route,data) => {calls.push({route,data}); return {code:null};},
    toDataURL:async () => 'data:image/png;base64,YQ==',
    launch:alias => {assert.match(alias,/^enrollment-1-/); return child;},
    pause:async () => {if (!pauses++) child.stdout.emit('data',Buffer.from('https://liteapp.weixin.qq.com/a?qrcode=one\n'));
      else {child.stdout.emit('data',Buffer.from('已将此 OpenClaw 连接到微信。')); child.emit('close',0);}},
  });
  assert.equal(calls.filter(v => v.route.endsWith('/complete')).length,1);
  const complete = calls.find(v => v.route.endsWith('/complete')); assert.equal(complete.data.qr_image,undefined);
  assert.equal(complete.data.sender_id,'Bob');
});
test('cancelled lease terminates only own native child without admitting', async () => {
  const child = fakeChild(); let completed=false;
  await assert.rejects(runEnrollment({id:1,lease:'a'.repeat(64)}, {
    snapshot:async () => new Map(),launch:() => child,toDataURL:async () => '',pause:async () => {},
    call:async route => {if(route.endsWith('/complete')) completed=true; const error=new Error('cancelled');error.status=409;throw error;},
  }));
  assert.equal(child.killed,true); assert.equal(completed,false);
});
test('lost completion HTTP acknowledgment retries admission, never launches twice', async () => {
  const child=fakeChild(); let snapshots=0, launches=0, completed=0;
  await runEnrollment({id:1,lease:'a'.repeat(64)}, {
    snapshot:async () => snapshots++ ? new Map([['b',{account_id:'b',sender_id:'Bob',fingerprint:'new'}]]) : new Map(),
    launch:() => {launches++; return child;}, toDataURL:async () => '',
    pause:async () => {child.stdout.emit('data',Buffer.from('已将此 OpenClaw 连接到微信。')); child.emit('close',0);},
    call:async route => {if(route.endsWith('/complete') && ++completed===1) throw new Error('network'); return {code:null};},
  });
  assert.equal(launches,1); assert.equal(completed,2);
});
test('pairing code forwarded to exact owned CLI stdin and not to completion', async () => {
  const child=fakeChild(); let snapshots=0, polls=0;
  await runEnrollment({id:1,lease:'a'.repeat(64)}, {
    snapshot:async () => snapshots++ ? new Map([['b',{account_id:'b',sender_id:'Bob',fingerprint:'new'}]]) : new Map(),
    launch:() => child,toDataURL:async () => '',
    call:async (route,data) => {if (route.endsWith('/poll')) return {code:polls++===1 ? '5678' : null};
      if (route.endsWith('/complete')) assert.equal(data.code,undefined); return {};},
    pause:async () => {if (!child.code) child.stdout.emit('data',Buffer.from('输入手机微信显示的数字，以继续连接：'));
      else {child.stdout.emit('data',Buffer.from('已将此 OpenClaw 连接到微信。'));child.emit('close',0);}},
  });
  assert.equal(child.code,'5678\n');
});
