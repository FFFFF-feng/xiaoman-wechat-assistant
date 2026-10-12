import { readFile } from 'node:fs/promises';
import { createReminderPump } from './reminders.js';

const failure = '小满服务暂时没有响应。若刚刚创建或修改了任务，请先发“查询待办”确认结果，避免重复操作。';

export function createBridgeHandler(config, deps = {}) {
  const read = deps.readFile ?? readFile;
  const fetchLocal = deps.fetch ?? globalThis.fetch;
  const logger = deps.logger ?? { warn() {}, info() {} };
  return async (event, ctx) => {
    if (ctx.channelId !== 'openclaw-weixin') return;
    // This channel is owned even on errors: never fall back to the OpenClaw persona/model.
    const handled = text => ({ handled: true, text });
    try {
      if (event.isGroup) return handled('当前小满接入只支持已绑定用户的私聊。');
      if (config.backendUrl !== 'http://127.0.0.1:18083') throw new Error('invalid-backend');
      const account_id = ctx.accountId;
      // Weixin 2.4.10 sets From/To/OriginatingTo to the same private-chat peer,
      // but omits SenderId. The host projects that trusted peer as conversationId.
      // Do not infer identity from message text, a session-key substring or a default user.
      const sender_id = ctx.senderId ?? event.senderId ?? ctx.conversationId;
      const message_id = ctx.messageId ?? event.messageId;
      const content = event.content;
      if (ctx.conversationId && sender_id && ctx.conversationId !== sender_id) {
        return handled('消息发送者与私聊对象不一致，未执行操作。');
      }
      if (![account_id, sender_id, message_id].every(v => typeof v === 'string' && v.length > 0 && v.length <= 160)) {
        logger.warn(`Xiaoman identity diagnostics: ${JSON.stringify({
          accountPresent: typeof account_id === 'string' && !!account_id,
          senderPresent: typeof sender_id === 'string' && !!sender_id,
          messagePresent: typeof message_id === 'string' && !!message_id,
          eventKeys: Object.keys(event), contextKeys: Object.keys(ctx),
        })}`);
        return handled('小满没有收到完整的消息身份，未执行操作。请发送一条新的文字消息。');
      }
      const bindings = JSON.parse(await read(config.bindingsFile, 'utf8'));
      if (!Array.isArray(bindings) || !bindings.some(v => v.account_id === account_id && v.sender_id === sender_id)) {
        return handled('此微信用户尚未绑定小满，未读取或修改任何任务。');
      }
      if (event.mediaStagingPending || event.media?.length || event.originalMedia?.length) {
        return handled('当前版本只接入文字消息，图片、文件和合并聊天记录暂未接入。请先把需要处理的内容粘贴为文字。');
      }
      if (typeof content !== 'string' || !content.trim() || content.length > 8000) {
        return handled('请发送一条不超过 8000 字的文字消息。');
      }
      const token = (await read(config.tokenFile, 'utf8')).trim();
      if (!/^[a-f0-9]{64}$/.test(token)) throw new Error('invalid-token-file');
      const response = await fetchLocal(`${config.backendUrl}/api/internal/clawbot/chat`, {
        method: 'POST', headers: { 'content-type': 'application/json', authorization: `Bearer ${token}` },
        body: JSON.stringify({ account_id, sender_id, message_id, content }),
        redirect: 'error', signal: AbortSignal.timeout(65000),
      });
      if (!response.ok) {
        logger.warn(`Xiaoman bridge HTTP ${response.status}; no retry or model fallback.`);
        if (response.status === 429) return handled('消息发送较频繁，请等一分钟再试。此次没有执行新操作。');
        return handled(failure);
      }
      const reader = response.body.getReader();
      const chunks = [];
      let length = 0;
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          length += value.length;
          if (length > 32768) throw new Error('reply-too-large');
          chunks.push(value);
        }
      } finally { await reader.cancel(); }
      const result = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      if (typeof result.reply !== 'string' || !result.reply.trim()) throw new Error('invalid-reply');
      logger.info('Xiaoman bridge handled Weixin text through the Python service.');
      return handled(result.reply.slice(0, 8000));
    } catch {
      logger.warn('Xiaoman bridge unavailable; no retry or model fallback.');
      return handled(failure);
    }
  };
}

export default {
  id: 'xiaoman-bridge',
  name: 'Xiaoman Python bridge',
  register(api) {
    api.on('before_dispatch', createBridgeHandler(api.pluginConfig, { logger: api.logger }),
      { priority: 100, timeoutMs: 75000 });
    api.registerService({
      id: 'xiaoman-reminders', apiVersion: 2,
      start(ctx) {
        const pump = createReminderPump(api.pluginConfig, {
          logger: ctx.logger, hostConfig: ctx.config,
          loadAdapter: id => api.runtime.channel.outbound.loadAdapter(id),
        });
        ctx.scheduler.schedule({ id: 'deliver-reminders', delayMs: 0, everyMs: 5000,
          run: () => pump.run(ctx.scheduler.signal) });
      },
    });
  },
};
