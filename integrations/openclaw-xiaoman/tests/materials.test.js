import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {createBridgeHandler} from '../index.js';
import {createMediaStore} from '../voice.js';
import {readMaterialData, MAX_MATERIAL_BYTES} from '../materials.js';

const ctx = {channelId:'openclaw-weixin',accountId:'bot',conversationId:'Alice',messageId:'image-1'};
const config = {backendUrl:'http://127.0.0.1:18083',tokenFile:'token',bindingsFile:'bindings'};
const event = {from:'Alice',content:'[图片]',media:[{kind:'image',contentType:'image/png',path:'/private/media/inbound/photo.png'}]};
function setup(eventOverride=event, dependencies={}) {
  const store = createMediaStore(); store.observe(eventOverride,ctx); const calls=[];
  const handler = createBridgeHandler(config, {
    mediaStore:store,
    readFile:async file => file==='token' ? 'a'.repeat(64) : JSON.stringify([{account_id:'bot',sender_id:'Alice'}]),
    readMaterialData:async () => ({kind:'image',filename:'photo.png',data_base64:'aW1hZ2U='}),
    fetch:async (url, options) => {calls.push({url,body:JSON.parse(options.body)});return Response.json({reply:'图片识别结果'});},
    ...dependencies,
  });
  return {handler,calls,store};
}

test('trusted staged image reaches the authenticated material route with exact identity', async () => {
  const {handler,calls} = setup();
  assert.match((await handler({content:'[图片]'},ctx)).text,/图片识别/);
  assert.match(calls[0].url,/\/materials$/);
  assert.deepEqual(calls[0].body,{account_id:'bot',sender_id:'Alice',message_id:'image-1',kind:'image',filename:'photo.png',data_base64:'aW1hZ2U='});
});
test('files bypass text admission and retain local source facts', async () => {
  const e = {...event,content:'[文件]',media:[{kind:'document',contentType:'application/pdf',path:'/private/media/inbound/sample.pdf'}]};
  const {handler,calls} = setup(e,{textQueue:{hasWaiting:()=>false,enqueue:()=>{throw new Error('file in text queue');}},
    readMaterialData:async () => ({kind:'file',filename:'sample.pdf',data_base64:'ZmlsZQ=='})});
  await handler({content:'[文件]'},ctx);
  assert.equal(calls[0].body.kind,'file');
});
test('pending, quoted, excess and inconsistent image facts fail without a backend call', async () => {
  for (const changed of [{...event,mediaStagingPending:true}, {...event,replyToIsQuote:true},
      {...event,media:Array(4).fill(event.media[0])}, {...event,content:'another'}]) {
    const {handler,calls}=setup(changed);
    assert.equal((await handler({content:'[图片]'},ctx)).handled,true);
    assert.equal(calls.length,0);
  }
});
test('late trusted hook facts are matched to the same source message', async () => {
  const {handler,calls,store} = setup({...event,media:[]});
  const pending = handler({content:'[图片]'},ctx);
  store.observe(event,ctx);
  await pending;
  assert.equal(calls.length,1);
});
test('image facts from another user never substitute for a missing attachment', async () => {
  const {handler,calls}=setup({...event,from:'Bob'});
  assert.match((await handler({content:'[图片]'},ctx)).text,/未收到/);
  assert.equal(calls.length,0);
});
test('waiting text blocks attachment reading and task execution', async () => {
  const {handler,calls}=setup(event,{textQueue:{hasWaiting:()=>true}});
  assert.match((await handler({content:'[图片]'},ctx)).text,/前面的文字/);
  assert.equal(calls.length,0);
});
test('native merged-card placeholder explains lack of body and is never task input', async () => {
  const {handler,calls}=setup({...event,media:[]});
  assert.match((await handler({content:'[聊天记录]'},ctx)).text,/未提供.*正文/);
  assert.equal(calls.length,0);
});
test('safe backend diagnostics are bounded, provider details are not echoed or retried', async () => {
  for (const response of [Response.json({detail:'正文超过24000字，请拆分文件或聊天记录后再发'},{status:422}),
      new Response('secret-provider-details',{status:422})]) {
    let n=0; const {handler}=setup(event,{fetch:async()=>{n++;return response;}});
    const result=await handler({content:'[图片]'},ctx);
    assert.match(result.text,/未创建提醒/);assert.doesNotMatch(result.text,/secret-provider/);assert.equal(n,1);
  }
});

async function fixture(t) {
  const directory=await fs.mkdtemp(path.join(os.tmpdir(),'xiaoman-material-test-'));
  const inbound=path.join(directory,'state/media/inbound');await fs.mkdir(inbound,{recursive:true});
  t.after(()=>fs.rm(directory,{recursive:true,force:true}));
  return {directory,inbound,config:{bindingsFile:path.join(directory,'state/xiaoman/bindings.json')}};
}
test('file reader supports bounded images, PDF and text only inside inbound storage', async t => {
  const f=await fixture(t);
  for (const [name,contentType,kind] of [['sample.png','image/png','image'],['sample.pdf','application/pdf','document'],['sample.txt','text/plain','file']]) {
    const filename=path.join(f.inbound,name);await fs.writeFile(filename,'fixture');
    const result=await readMaterialData(f.config,{path:filename,contentType,kind});
    assert.equal(result.filename,name);assert.equal(result.kind,kind==='image'?'image':'file');
    assert.equal(result.data_base64,Buffer.from('fixture').toString('base64'));
  }
});
test('outside files, remote URLs, executable formats and oversized files are rejected', async t => {
  const f=await fixture(t), local=path.join(f.inbound,'sample.txt'), outside=path.join(f.directory,'private.txt');
  await fs.writeFile(local,'fixture');await fs.writeFile(outside,'private');
  for (const fact of [{path:outside},{path:'https://evil.example/file.txt'}, {path:local,url:'https://evil.example/file.txt'},
      {path:path.join(f.inbound,'program.exe')}, {path:local,kind:'video'}]) {
    await assert.rejects(readMaterialData(f.config,fact));
  }
  await fs.writeFile(local,Buffer.alloc(MAX_MATERIAL_BYTES+1));
  await assert.rejects(readMaterialData(f.config,{path:local}));
});
test('host saved filename preserves exported XML format after generic MIME renaming', async t => {
  const f=await fixture(t), saved=path.join(f.inbound,'聊天.xml---12345678-1234-1234-1234-123456789abc.txt');
  await fs.writeFile(saved,'<recordinfo/>');
  const result=await readMaterialData(f.config,{path:saved,kind:'document',contentType:'text/plain'});
  assert.equal(result.filename,'聊天.xml');
});

test('Tencent image wildcard reaches the image reader without guessing from the filename', async t => {
  const f=await fixture(t);
  for (const name of ['photo.jpg','screenshot.png','photo.webp','host-image.bin']) {
    const saved=path.join(f.inbound,name);await fs.writeFile(saved,'fixture');
    const result=await readMaterialData(f.config,{path:saved,kind:'image',contentType:'image/*'});
    assert.equal(result.kind,'image');
    assert.equal(result.data_base64,Buffer.from('fixture').toString('base64'));
    await assert.rejects(readMaterialData(f.config,{path:saved,kind:'document',contentType:'image/*'}));
    await assert.rejects(readMaterialData(f.config,{path:saved,kind:'image',contentType:'application/octet-stream'}));
  }
});

test('real local wildcard attachment passes trusted hook, binding and material dispatch', async t => {
  const f=await fixture(t), saved=path.join(f.inbound,'phone-screenshot.jpg');
  await fs.writeFile(saved,'fixture');
  const e={...event,media:[{path:saved,kind:'image',contentType:'image/*'}]};
  const store=createMediaStore();store.observe(e,ctx);const calls=[];
  const handler=createBridgeHandler({...config,...f.config},{mediaStore:store,
    readFile:async file=>file==='token'?'a'.repeat(64):JSON.stringify([{account_id:'bot',sender_id:'Alice'}]),
    fetch:async (url,options)=>{calls.push({url,body:JSON.parse(options.body)});return Response.json({reply:'识别结果'});}});
  assert.equal((await handler({content:'[图片]'},ctx)).text,'识别结果');
  assert.equal(calls.length,1);assert.match(calls[0].url,/\/materials$/);
  assert.equal(calls[0].body.kind,'image');assert.equal(calls[0].body.filename,'phone-screenshot.jpg');
});
