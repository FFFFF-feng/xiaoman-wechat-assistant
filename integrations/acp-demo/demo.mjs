import {mkdtemp} from 'node:fs/promises';
import {randomBytes} from 'node:crypto';
import os from 'node:os';
import path from 'node:path';
import {once} from 'node:events';
import {createDemoServer} from './service.mjs';

const runtime=process.argv.includes('--codex')?'codex':'fixture';
const directory=await mkdtemp(path.join(os.tmpdir(),'xiaoman-acp-demo-'));
const token=randomBytes(32).toString('hex');
const server=createDemoServer({token,directory,runtime,timeoutMs:45000});
server.listen(0,'127.0.0.1');await once(server,'listening');
const url=`http://127.0.0.1:${server.address().port}/demo/chat`;
const payload={account_id:'demo-bot',sender_id:'Alice',message_id:'synthetic-1',
  content:'中午提醒我取快递。先给一个建议时间，再给两个备选方案；这里只演示，不要创建提醒。'};
try {
  console.log(`运行模式：${runtime}；本地接口：${url}`);
  console.log(`输入：${payload.content}`);
  const response=await fetch(url,{method:'POST',headers:{'content-type':'application/json',authorization:'Bearer '+token},
    body:JSON.stringify(payload),signal:AbortSignal.timeout(55000)});
  const result=await response.json();
  console.log(JSON.stringify(result,null,2));
  if(response.status!==200)throw new Error('demo-request-failed');
  const duplicate=await fetch(url,{method:'POST',headers:{'content-type':'application/json',authorization:'Bearer '+token},body:JSON.stringify(payload)});
  if(JSON.stringify(await duplicate.json())!==JSON.stringify(result))throw new Error('dedup-failed');
  console.log('PASS：ACP调用完成；同一消息重复请求复用结果；未写入任务，未发送微信。');
}catch(error){console.error('DEMO FAIL: '+error.message);process.exitCode=1;}
finally{server.close();server.closeAllConnections();await once(server,'close');}
