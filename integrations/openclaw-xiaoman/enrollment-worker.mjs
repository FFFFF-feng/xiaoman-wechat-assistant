/** Local sidecar. XIAOMAN_LOCAL_ROOT points at the owned isolated OpenClaw installation. */
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
import {createServer} from 'node:net';
import {setTimeout as pause} from 'node:timers/promises';
import {isolatedEnvironment, registrySnapshot, runEnrollment} from './enrollment.js';

const root = path.resolve(process.env.XIAOMAN_LOCAL_ROOT || '');
if (!process.env.XIAOMAN_LOCAL_ROOT) throw new Error('An isolated local root is required.');
const stateDir = path.join(root,'state');
const privateDir = path.join(stateDir,'xiaoman');
if (!fs.existsSync(path.join(privateDir,'.xiaoman-bridge'))) throw new Error('Owned bridge runtime required.');
const cli = path.join(root,'npm','node_modules','openclaw','openclaw.mjs');
const config = JSON.parse(fs.readFileSync(path.join(stateDir,'openclaw.json'),'utf8'));
if (config.session?.dmScope !== 'per-account-channel-peer' || config.gateway?.bind !== 'loopback' ||
    config.gateway?.auth?.mode !== 'token' || config.gateway?.controlUi?.enabled !== false) throw new Error('Unsafe gateway configuration.');
const token = fs.readFileSync(path.join(privateDir,'bridge.key'),'utf8').trim();
if (!/^[a-f0-9]{64}$/.test(token)) throw new Error('Invalid private bridge token.');
const require = createRequire(path.join(path.dirname(cli),'package.json'));
const qrcode = require('qrcode');
// Exclusive OS socket prevents two rapid launcher clicks from creating two workers.
// No API: loopback-only, every incoming connection closes immediately.
const singleton = createServer(socket => socket.destroy());
await new Promise((resolve,reject) => {
  singleton.once('error', () => reject(new Error('Enrollment singleton port 18795 is already occupied.')));
  singleton.listen(18795,'127.0.0.1',resolve);
});
const pidFile = path.join(privateDir,'enrollment-process.json');
fs.writeFileSync(pidFile, JSON.stringify({pid:process.pid, started_at:new Date().toISOString()}));
const env = isolatedEnvironment(process.env, root, stateDir);
// Avoid compile-cache respawn: cancellation must address the exact CLI process we spawned.
env.NODE_DISABLE_COMPILE_CACHE = '1';
const nativeChildren = new Set();
let stopping = false;
function stop() {
  if (stopping) return;
  stopping = true;
  singleton.close();
  for (const child of nativeChildren) child.kill();
  try {if (JSON.parse(fs.readFileSync(pidFile,'utf8')).pid === process.pid) fs.unlinkSync(pidFile);} catch {}
}
process.on('SIGINT',stop); process.on('SIGTERM',stop); process.on('exit',stop);
async function call(route, data) {
  const response = await fetch(`http://127.0.0.1:18083${route}`, {method:'POST',
    headers:{Authorization:`Bearer ${token}`, 'Content-Type':'application/json'},
    body:JSON.stringify(data), redirect:'error', signal:AbortSignal.timeout(10000)});
  if (!response.ok) {const error = new Error('local-bridge-error'); error.status = response.status; throw error;}
  const text = await response.text();
  if (text.length > 200000) throw new Error('response-too-large');
  return JSON.parse(text);
}
console.log('Invitation enrollment worker online. Native login output stays in memory.');
while (!stopping) {
  try {
    const {job} = await call('/api/internal/clawbot/enrollment/claim', {});
    if (job) await runEnrollment(job, {call, pause, snapshot:() => registrySnapshot(stateDir),
      toDataURL:(url, options) => qrcode.toDataURL(url, options),
      launch:account => {
        const child = spawn(process.execPath,[cli,'channels','login','--channel','openclaw-weixin','--account',account],
          {env, cwd:root, windowsHide:true, stdio:['pipe','pipe','pipe']});
        nativeChildren.add(child); child.on('close',() => nativeChildren.delete(child)); return child;
      }});
  } catch {console.warn('Enrollment temporarily unavailable or stopped; no automatic native-login retry.');}
  await pause(3000);
}
