import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { createStickerPump } from '../reminders.js';
import { resolveSticker } from '../sticker-assets.js';

const config={backendUrl:'http://127.0.0.1:18083',tokenFile:'token',bindingsFile:'bindings'};
function setup({outcome='accepted',rejectStart=false,unbound=false,failAck=false,sticker='received',noMedia=false}={}) {
  let claimed=false, sends=0, result;
  const paths=[];
  const pump=createStickerPump(config,{
    readFile:async p=>p==='token'?'a'.repeat(64):JSON.stringify([{account_id:'bot',sender_id:unbound?'Bob':'Alice'}]),
    hostConfig:{},
    loadAdapter:async()=>noMedia?{sendText:()=>{throw Error('must not replace image with text');}}:{
      sendMedia:async ctx=>{
        sends++;
        assert.equal(ctx.to,'Alice'); assert.equal(ctx.accountId,'bot'); assert.equal(ctx.text,'');
        assert.ok(path.isAbsolute(ctx.mediaUrl)); assert.equal(path.basename(ctx.mediaUrl),'received.png');
        assert.deepEqual(ctx.mediaLocalRoots,[path.dirname(ctx.mediaUrl)]);
        if(outcome==='unknown')throw Error('private-provider-token');
        return {messageId:outcome==='empty'?'':'synthetic-id'};
      }},
    fetch:async(url,options)=>{
      assert.equal(options.redirect,'error');
      assert.equal(options.headers.authorization,'Bearer '+'a'.repeat(64));
      const p=new URL(url).pathname; paths.push(p);
      assert.ok(p.startsWith('/api/internal/clawbot/stickers/'));
      if(p.endsWith('/claim')) {
        const delivery=claimed?null:{id:1,claim_token:'b'.repeat(64),account_id:'bot',sender_id:'Alice',text:'',sticker_id:sticker};
        claimed=true;return Response.json({delivery});
      }
      if(p.endsWith('/start'))return Response.json({send:true},{status:rejectStart?409:200});
      if(p.endsWith('/result')){result=JSON.parse(options.body);return Response.json({ok:true},{status:failAck?500:200});}
      throw Error('unexpected-path');
    }
  });
  return {pump,paths,sends:()=>sends,result:()=>result};
}

test('fixed generated asset IDs resolve to verified local PNGs',async()=>{
  for(const id of ['morning','received','cheer','rest','thanks','goodnight']) {
    const asset=await resolveSticker(id);assert.equal(path.basename(asset.file),id+'.png');
  }
  for(const id of ['../../.env','https://attacker/image.png','file:///secret','unknown'])await assert.rejects(()=>resolveSticker(id));
});

test('native sendMedia uses exact account and bound recipient once',async()=>{
  const t=setup();await t.pump.run();await t.pump.run();
  assert.equal(t.sends(),1);assert.equal(t.result().outcome,'accepted');assert.equal(t.result().message_id,'synthetic-id');
});

test('no sendMedia capability does not claim or fall back to text',async()=>{
  const t=setup({noMedia:true});await t.pump.run();assert.equal(t.paths.length,0);assert.equal(t.sends(),0);
});

test('binding removal or denied start prevents native upload',async()=>{
  for(const options of [{unbound:true},{rejectStart:true}]){const t=setup(options);await t.pump.run();assert.equal(t.sends(),0);assert.equal(t.result(),undefined);}
});

test('unlisted sticker cannot access arbitrary paths or start delivery',async()=>{
  const t=setup({sticker:'../../.env'});await t.pump.run();
  assert.equal(t.sends(),0);assert.equal(t.paths.length,1);
});

test('native exception is unknown and never retried',async()=>{
  const t=setup({outcome:'unknown'});await t.pump.run();await t.pump.run();
  assert.equal(t.sends(),1);assert.equal(t.result().outcome,'unknown');
});

test('empty receipt is failure and failed ACK does not repeat upload',async()=>{
  const t=setup({outcome:'empty'});await t.pump.run();assert.equal(t.result().outcome,'failed');
  const ack=setup({failAck:true});await ack.pump.run();await ack.pump.run();assert.equal(ack.sends(),1);
});

test('aborted service has no provider or backend operations',async()=>{
  const t=setup();await t.pump.run(AbortSignal.abort());assert.equal(t.paths.length,0);assert.equal(t.sends(),0);
});

test('asset hash, MIME bytes and path manifest are checked before upload',async()=>{
  const root=await mkdtemp(path.join(os.tmpdir(),'xiaoman-sticker-'));
  const png=Buffer.from([137,80,78,71,13,10,26,10]);
  const digest=createHash('sha256').update(png).digest('hex');
  const manifest=entry=>writeFile(path.join(root,'manifest.json'),JSON.stringify({received:entry}));
  try {
    await writeFile(path.join(root,'received.png'),png);
    await manifest({file:'received.png',sha256:digest});
    assert.equal(path.basename((await resolveSticker('received',root)).file),'received.png');
    await writeFile(path.join(root,'received.png'),'modified');await assert.rejects(()=>resolveSticker('received',root));
    await manifest({file:'../secret.png',sha256:digest});await assert.rejects(()=>resolveSticker('received',root));
  } finally {
    assert.equal(path.dirname(path.resolve(root)),path.resolve(os.tmpdir()));
    assert.ok(path.basename(root).startsWith('xiaoman-sticker-'));
    await rm(root,{recursive:true,force:true});
  }
});
