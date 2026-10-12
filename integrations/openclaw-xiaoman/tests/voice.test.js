import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {createBridgeHandler} from '../index.js';
import {createMediaStore, readVoiceData, MAX_AUDIO_BYTES} from '../voice.js';

const config = {backendUrl:'http://127.0.0.1:18083', tokenFile:'token', bindingsFile:'bindings'};
const context = {channelId:'openclaw-weixin', accountId:'test-bot', conversationId:'Alice', messageId:'voice-1'};
const event = {content:'[语音]', from:'Alice', messageId:'voice-1',
  media:[{kind:'audio', contentType:'audio/wav', path:'/private/media/inbound/fixture.wav'}]};
function setup(store, overrides = {}) {
  const calls = [];
  return {calls, handler:createBridgeHandler(config, {
    mediaStore:store,
    readFile:async file => file === 'token' ? 'a'.repeat(64) : JSON.stringify([{account_id:'test-bot',sender_id:'Alice'}]),
    readVoiceData:async () => 'fixture-base64',
    fetch:async (url, options) => {calls.push({url,options});return Response.json({reply:'语音识别：查询待办\n\n暂无任务'});},
    ...overrides,
  })};
}

test('matching trusted message_received audio reaches voice endpoint, not a body/path guess', async () => {
  const store = createMediaStore(); store.observe(event, context);
  const {handler,calls} = setup(store);
  const result = await handler({content:'[语音]'}, context);
  assert.match(result.text, /语音识别/);
  assert.match(calls[0].url, /\/voice$/);
  assert.deepEqual(JSON.parse(calls[0].options.body), {account_id:'test-bot',sender_id:'Alice',message_id:'voice-1',audio_base64:'fixture-base64'});
  assert.equal(calls[0].options.redirect,'error');
});
test('native voice transcript follows existing text-intent route without paying for ASR again', async () => {
  const {handler,calls} = setup(createMediaStore());
  await handler({content:'两分钟后提醒我喝水'}, context);
  assert.match(calls[0].url, /\/chat$/);
  assert.equal(JSON.parse(calls[0].options.body).content,'两分钟后提醒我喝水');
});
test('media facts require exact identity, are bounded and expire', () => {
  let time = 0; const store = createMediaStore({now:() => time});
  store.observe(event, {...context,conversationId:'Bob'});
  assert.equal(store.get('test-bot','Bob','voice-1'),undefined);
  store.observe(event, context);
  assert.ok(store.get('test-bot','Alice','voice-1'));
  assert.equal(store.get('other-bot','Alice','voice-1'),undefined);
  for(let i=0;i<501;i++) store.observe({...event,messageId:String(i)}, {...context,messageId:String(i)});
  assert.equal(store.get('test-bot','Alice','voice-1'),undefined);
  time = 90001;
  assert.equal(store.get('test-bot','Alice','500'),undefined);
});
test('quoted, pending, mixed or inconsistent attachment facts never execute intentions', async () => {
  for (const changed of [{...event,replyToIsQuote:true}, {...event,mediaStagingPending:true},
      {...event,media:[...event.media,{kind:'image'}]}, {...event,content:'different'}]) {
    const store = createMediaStore(); store.observe(changed,context);
    const {handler,calls} = setup(store);
    assert.equal((await handler({content:'[语音]'},context)).handled,true);
    assert.equal(calls.length,0);
  }
});
test('missing audio facts or read failure does not call backend or default agent', async () => {
  const missing = setup();
  assert.match((await missing.handler({content:'[语音]'},context)).text,/未执行/);
  assert.equal(missing.calls.length,0);
  const store = createMediaStore();store.observe(event,context);
  const failed = setup(store,{readVoiceData:async () => {throw new Error('private path');}});
  assert.match((await failed.handler({content:'[语音]'},context)).text,/未执行/);
  assert.equal(failed.calls.length,0);
});
test('ASR rejection is not retried and provider error content is never echoed', async () => {
  const store = createMediaStore();store.observe(event,context);let calls=0;
  const {handler} = setup(store,{fetch:async () => {calls++;return new Response('private-provider-data',{status:422});}});
  const result = await handler({content:'[语音]'},context);
  assert.match(result.text,/未执行/);assert.doesNotMatch(result.text,/private-provider-data/);assert.equal(calls,1);
});

async function files(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(),'xiaoman-voice-test-'));
  t.after(() => fs.rm(root,{recursive:true,force:true}));
  const inbound = path.join(root,'state/media/inbound');await fs.mkdir(inbound,{recursive:true});
  const data = Buffer.alloc(48);data.write('RIFF',0);data.write('WAVE',8);
  const wav = path.join(inbound,'sample.wav');await fs.writeFile(wav,data);
  return {root,inbound,wav,data,settings:{bindingsFile:path.join(root,'state/xiaoman/bindings.json')}};
}
test('reads only a bounded WAV under the isolated inbound media directory', async t => {
  const f = await files(t);
  assert.equal(await readVoiceData(f.settings,{path:f.wav,contentType:'audio/wav',kind:'audio'}), f.data.toString('base64'));
});
test('outside media root, remote URLs and wrong MIME are rejected', async t => {
  const f = await files(t), outside = path.join(f.root,'private.wav');await fs.writeFile(outside,f.data);
  for (const fact of [{path:outside,contentType:'audio/wav'}, {path:'https://evil.example/audio.wav',contentType:'audio/wav'},
      {path:f.wav,contentType:'audio/silk'}, {path:f.wav,contentType:'audio/wav',url:'https://evil.example/audio.wav'}]) {
    await assert.rejects(readVoiceData(f.settings,fact));
  }
});
test('oversized or invalid WAV never leaves the local file reader', async t => {
  const f = await files(t);
  await fs.writeFile(f.wav,Buffer.alloc(MAX_AUDIO_BYTES+1));
  await assert.rejects(readVoiceData(f.settings,{path:f.wav,contentType:'audio/wav'}));
  await fs.writeFile(f.wav,Buffer.alloc(48));
  await assert.rejects(readVoiceData(f.settings,{path:f.wav,contentType:'audio/wav'}));
});
