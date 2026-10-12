/** Official CLI enrollment only. No private Weixin HTTP protocol or agent tools. */
import {readFile} from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {StringDecoder} from 'node:string_decoder';

export function createLoginParser() {
  const decoder = new StringDecoder('utf8');
  let tail = '', success = false;
  return {
    get success() {return success;},
    push(chunk) {
      tail = (tail + decoder.write(Buffer.from(chunk))).replace(/\x1b\[[0-9;]*m/g, '').slice(-16000);
      if (tail.includes('已将此 OpenClaw 连接到微信。')) success = true;
      const events = [];
      const qr = /https:\/\/liteapp\.weixin\.qq\.com\/[^\s]+(?=[\r\n])/g;
      let match, consumed = 0;
      while ((match = qr.exec(tail))) {
        const url = new URL(match[0]);
        if (url.hostname === 'liteapp.weixin.qq.com' && !url.username && !url.password && url.searchParams.has('qrcode')) {
          events.push({type:'qr', url:match[0]});
        }
        consumed = qr.lastIndex;
      }
      for (const prompt of ['输入手机微信显示的数字，以继续连接：', '你输入的数字不匹配，请重新输入：']) {
        const at = tail.indexOf(prompt);
        if (at >= 0) {events.push({type:'needs_code'}); consumed = Math.max(consumed, at + prompt.length);}
      }
      if (consumed) tail = tail.slice(consumed);
      return events;
    },
  };
}

export async function registrySnapshot(stateDir, read = readFile) {
  const root = path.join(stateDir, 'openclaw-weixin');
  let index;
  try {index = await read(path.join(root, 'accounts.json'), 'utf8');}
  catch (error) {
    // The official plugin creates this index on its first successful login.
    // Only a missing index means a fresh installation; damaged/partial state must fail closed.
    if (error.code === 'ENOENT') return new Map();
    throw error;
  }
  const ids = JSON.parse(index);
  if (!Array.isArray(ids) || ids.length > 200 || ids.some(id => typeof id !== 'string' || !/^[a-z0-9_-]{1,160}$/.test(id))) {
    throw new Error('invalid-registry');
  }
  const result = new Map();
  for (const id of ids) {
    const value = JSON.parse(await read(path.join(root, 'accounts', `${id}.json`), 'utf8'));
    const url = new URL(value.baseUrl);
    if (typeof value.token !== 'string' || !value.token || typeof value.userId !== 'string' ||
        !/^[^\x00-\x20\x7f]{1,160}$/.test(value.userId) || url.protocol !== 'https:' ||
        !/^ilinkai[0-9]*\.weixin\.qq\.com$/.test(url.hostname) || url.username || url.password) {
      throw new Error('invalid-native-account');
    }
    result.set(id, {account_id:id, sender_id:value.userId,
      fingerprint:crypto.createHash('sha256').update(`${value.token}\0${value.userId}`).digest('hex')});
  }
  return result;
}

export function confirmedIdentity(before, after, success) {
  if (!success) throw new Error('login-unconfirmed');
  const changed = [...after.values()].filter(v => before.get(v.account_id)?.fingerprint !== v.fingerprint);
  if (changed.length !== 1) throw new Error('identity-ambiguous');
  const value = changed[0];
  if ([...before.values()].some(old => old.sender_id === value.sender_id)) throw new Error('existing-user-relogin');
  return {account_id:value.account_id, sender_id:value.sender_id};
}

export function isolatedEnvironment(source, root, stateDir) {
  const env = {...source};
  for (const name of Object.keys(env)) {
    if (/^(OPENCLAW_|WECHAT_|MODEL_|DASHSCOPE_|ALIYUN_|XIAOMAN_|CLAWBOT_)/.test(name) ||
        /(^|_)(API_KEY|AUTH_TOKEN|ACCESS_TOKEN|SECRET|PASSWORD)$/.test(name) ||
        /^(ALL|HTTP|HTTPS)_PROXY$/i.test(name) || ['GH_TOKEN','GITHUB_TOKEN','QWEN_OAUTH_TOKEN'].includes(name)) delete env[name];
  }
  Object.assign(env, {OPENCLAW_HOME:path.join(root,'home'), OPENCLAW_STATE_DIR:stateDir,
    OPENCLAW_CONFIG_PATH:path.join(stateDir,'openclaw.json'), OPENCLAW_WORKSPACE_DIR:path.join(root,'workspace'),
    NODE_COMPILE_CACHE:path.join(root,'node-cache'), npm_config_prefix:path.join(root,'npm'),
    npm_config_cache:path.join(root,'npm-cache'), npm_config_userconfig:path.join(root,'.npmrc')});
  return env;
}

export async function runEnrollment(job, deps) {
  const {call, launch, snapshot, toDataURL, pause} = deps;
  const before = await snapshot();
  const parser = createLoginParser();
  const events = [];
  const child = launch(`enrollment-${job.id}-${crypto.randomBytes(6).toString('hex')}`);
  let exit = null, childError = false;
  child.stdout.on('data', data => events.push(...parser.push(data)));
  child.stderr.on('data', () => {}); // Never publish vendor logs, account IDs, session keys or QR URLs.
  child.on('error', () => {childError = true;});
  child.on('close', code => {exit = {code};});
  const prefix = `/api/internal/clawbot/enrollment/${job.id}`;
  const deadline = Date.now() + 8 * 60 * 1000;
  try {
    while (!exit && !childError) {
      if (Date.now() >= deadline) throw new Error('login-timeout');
      const command = await call(`${prefix}/poll`, {lease:job.lease});
      if (command.code) child.stdin.write(`${command.code}\n`);
      for (const event of events.splice(0, 16)) {
        if (event.type === 'qr') await call(`${prefix}/update`, {lease:job.lease, state:'awaiting_scan',
          qr_image:await toDataURL(event.url, {width:360, margin:4, errorCorrectionLevel:'M'})});
        else await call(`${prefix}/update`, {lease:job.lease, state:'needs_code'});
      }
      await pause(1500);
    }
    if (childError || exit?.code !== 0) throw new Error('login-failed');
    const identity = confirmedIdentity(before, await snapshot(), parser.success);
    // Retrying this idempotent acknowledgment never launches or sends a second native login.
    let lastError;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {await call(`${prefix}/complete`, {lease:job.lease, ...identity}); return;}
      catch (error) {lastError = error; if ([403,409].includes(error.status)) throw error; await pause(1500);}
    }
    throw lastError;
  } catch (error) {
    if (!exit && !child.killed) child.kill(); // Only this worker's own login subprocess.
    const error_code = error.message === 'login-timeout' ? 'login_timeout' :
      ['identity-ambiguous','existing-user-relogin','login-unconfirmed'].includes(error.message) ? 'identity_ambiguous' : 'login_failed';
    try {await call(`${prefix}/update`, {lease:job.lease, state:'failed', error_code});} catch {}
    throw new Error(error_code); // Do not leak response bodies or vendor error text.
  } finally {
    if (!exit && !child.killed) child.kill();
  }
}
