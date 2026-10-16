// An isolated localhost adapter with the same identity envelope as the Python
// bridge. This is not registered in the production Weixin plugin.
import http from 'node:http';
import {timingSafeEqual,createHash,randomUUID} from 'node:crypto';
import path from 'node:path';
import {runAgent} from './runner.mjs';

export function createDemoServer({token,directory,runtime='fixture',run=runAgent,timeoutMs=45000}) {
  if(typeof token!=='string'||token.length<32)throw new Error('missing-demo-token');
  const receipts=new Map(),busy=new Set();
  const bindings=new Set(['demo-bot\0Alice','demo-bot\0Bob']);
  const send=(res,status,body)=>{
    res.writeHead(status,{'content-type':'application/json; charset=utf-8','cache-control':'no-store'});
    res.end(JSON.stringify(body));
  };
  return http.createServer(async(req,res)=>{
    if(req.socket.remoteAddress!=='127.0.0.1')return send(res,403,{error:'loopback-only'});
    const supplied=Buffer.from(req.headers.authorization||''),expected=Buffer.from('Bearer '+token);
    if(supplied.length!==expected.length||!timingSafeEqual(supplied,expected))return send(res,401,{error:'unauthorized'});
    if(req.method!=='POST'||req.url!=='/demo/chat')return send(res,404,{error:'not-found'});
    if(!/^application\/json(?:;|$)/i.test(req.headers['content-type']||''))return send(res,415,{error:'json-required'});
    let payload;
    try {
      let raw='';req.setEncoding('utf8');
      for await(const chunk of req){raw+=chunk;if(Buffer.byteLength(raw)>16384){send(res,413,{error:'body-too-large'});return;}}
      payload=JSON.parse(raw);
    }catch{return send(res,400,{error:'invalid-json'});}
    const fields=['account_id','sender_id','message_id','content'];
    if(!payload||Array.isArray(payload)||Object.keys(payload).length!==4||fields.some(k=>typeof payload[k]!=='string')||
       fields.slice(0,3).some(k=>!/^[^\s\x00-\x1f\x7f]{1,160}$/.test(payload[k]))||
       !payload.content.trim()||payload.content.length>2000)return send(res,422,{error:'invalid-fields'});
    const owner=payload.account_id+'\0'+payload.sender_id;
    if(!bindings.has(owner))return send(res,403,{error:'unbound-demo-user'});
    const key=owner+'\0'+payload.message_id;
    const fingerprint=createHash('sha256').update(payload.content).digest('hex');
    const prior=receipts.get(key);
    if(prior){
      if(prior.fingerprint!==fingerprint)return send(res,409,{error:'message-id-conflict'});
      const result=await prior.promise;return send(res,result.status,result.body);
    }
    if(receipts.size>=100)return send(res,503,{error:'demo-receipt-capacity'});
    if(busy.has(owner))return send(res,409,{error:'same-user-busy'});
    if(busy.size>=2)return send(res,429,{error:'demo-busy'});
    busy.add(owner);
    const operation=(async()=>{
      try {
        const ownerHash=createHash('sha256').update(owner).digest('hex');
        const result=await run({runtime,timeoutMs,directory:path.join(directory,ownerHash,randomUUID()),content:payload.content});
        return {status:200,body:{action:'acp_demo_reply',runtime,reply:result.reply,
          elapsed_ms:result.elapsedMs,
          task_write:false,wechat_send:false}};
      }catch(error){
        return {status:error.message==='agent-timeout'?504:502,body:{error:error.message==='agent-timeout'?'agent-timeout':'agent-unavailable',
          task_write:false,wechat_send:false}};
      }finally{busy.delete(owner);}
    })();
    receipts.set(key,{fingerprint,promise:operation});
    const result=await operation;send(res,result.status,result.body);
  });
}
