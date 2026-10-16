// Deterministic ACP v1 peer, NOT a model. It exercises the real acpx transport.
import * as acp from '@agentclientprotocol/sdk';
import {Readable, Writable} from 'node:stream';
import {randomUUID} from 'node:crypto';

const sessions = new Set();
acp.agent({name:'xiaoman-fixture'})
  .onRequest('initialize', () => ({protocolVersion:acp.PROTOCOL_VERSION,
    agentCapabilities:{loadSession:false}, agentInfo:{name:'xiaoman-fixture',version:'0.1.0'},authMethods:[]}))
  .onRequest('session/new', () => {
    const sessionId=randomUUID();sessions.add(sessionId);return {sessionId};
  })
  .onRequest('session/prompt', async ({params,client}) => {
    if (!sessions.has(params.sessionId)) throw new Error('unknown-session');
    const source=JSON.parse(params.prompt.filter(p=>p.type==='text').map(p=>p.text).join(''));
    if(source.content==='超时探针')await new Promise(()=>{});
    // Deliberately emit a thought: the HTTP bridge must never return it.
    await client.notify('session/update',{sessionId:params.sessionId,update:{
      sessionUpdate:'agent_thought_chunk',content:{type:'text',text:'fixture-internal-thought'}}});
    if (source.content==='权限探针') {
      const result=await client.request('session/request_permission',{sessionId:params.sessionId,
        toolCall:{toolCallId:'probe',title:'fixture permission probe',kind:'execute',status:'pending'},
        options:[{optionId:'allow',kind:'allow_once',name:'Allow'},{optionId:'deny',kind:'reject_once',name:'Deny'}]});
      const denied=result.outcome.outcome==='cancelled'||result.outcome.optionId==='deny';
      await client.notify('session/update',{sessionId:params.sessionId,update:{sessionUpdate:'agent_message_chunk',
        content:{type:'text',text:denied?'权限请求已拒绝；未执行任何工具。':'UNEXPECTED_APPROVAL'}}});
    } else {
      for (const text of ['[离线ACP样例] 我先建议中午12:00，', '也可以选12:30或18:00。这里只给建议，没有创建提醒。\n你说的是：'+source.content]) {
        await client.notify('session/update',{sessionId:params.sessionId,update:{
          sessionUpdate:'agent_message_chunk',content:{type:'text',text}}});
      }
    }
    return {stopReason:'end_turn'};
  })
  .connect(acp.ndJsonStream(Writable.toWeb(process.stdout),Readable.toWeb(process.stdin)));
