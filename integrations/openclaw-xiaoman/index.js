import { readFile } from 'node:fs/promises';
import { createReminderPump, createWelcomePump, createStickerPump, createAnnouncementPump } from './reminders.js';
import { createMediaStore, readVoiceData } from './voice.js';
import { readMaterialData } from './materials.js';
import path from 'node:path';
import { createTextBatchQueue, createTextBatchPump } from './text-batches.js';

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
      // before_dispatch has no media fields in OpenClaw 2026.10.1. Retain ONLY
      // matching trusted message_received facts in bounded short-lived memory.
      let media = deps.mediaStore?.get(account_id, sender_id, message_id);
      if (!media && /^\[(?:语音|图片|文件)\]$/.test(content) && deps.mediaStore) {
        await new Promise(resolve => setTimeout(resolve, 100));
        media = deps.mediaStore.get(account_id, sender_id, message_id);
      }
      if (media && media.content !== content) return handled('附件消息信息不一致，未执行操作，请重新发送。');
      const attachments = media?.media ?? event.media ?? [];
      let audio;
      let material;
      if (media?.pending || event.mediaStagingPending || event.originalMedia?.length || media?.quoted || event.replyToIsQuote) {
        return handled('附件尚未就绪或属于引用消息，未执行操作，请发文字。');
      }
      if (attachments.length) {
        if (attachments.length !== 1) return handled('请一次发送一份图片或文件，暂不支持混合附件。未执行操作。');
        if (content === '[语音]' && attachments[0].contentType === 'audio/wav') {
          try {audio = await (deps.readVoiceData ?? readVoiceData)(config, attachments[0]);}
          catch {return handled('语音文件不可用或格式不支持，未执行操作，请发送短语音或文字。');}
        } else {
          if (content === '[语音]' || attachments[0].contentType?.startsWith('audio/') || attachments[0].contentType?.startsWith('video/')) {
            return handled('此音视频格式暂不支持，请发送文字或清晰图片。未执行操作。');
          }
          try {material = await (deps.readMaterialData ?? readMaterialData)(config, attachments[0]);}
          catch {return handled('附件不可用、超过10MB或格式不支持。请发JPEG/PNG/WebP图片，或TXT、PDF、DOCX、XLSX、CSV、Markdown、JSON、聊天记录XML文件。未执行操作。');}
        }
      } else if (content === '[语音]' || /^\[(?:图片|视频|文件)\]$/.test(content)) {
        return handled('小满未收到可识别的附件内容，未执行操作，请发文字。');
      }
      if (/^\[(?:聊天记录|合并转发(?:聊天记录)?|转发聊天记录)\]$/.test(content?.trim())) {
        return handled('微信未提供这张合并转发卡片的正文，暂时无法读取。请发送聊天截图或导出的记录文件，或先发“整理聊天记录”再粘贴文字。');
      }
      if (typeof content !== 'string' || !content.trim() || content.length > 8000) {
        return handled('请发送一条不超过 8000 字的文字消息。');
      }
      if (!audio && !material && deps.textQueue) {
        // Fast durable admission: waiting here would block Tencent's sequential
        // monitor and prevent subsequent fragments from reaching the buffer.
        deps.textQueue.enqueue({ account_id, sender_id, message_id, content });
        return { handled: true }; // Host supports silent takeover; no default model.
      }
      if ((audio || material) && deps.textQueue?.hasWaiting(account_id, sender_id)) {
        return handled('前面的文字还在整理，等我回复后再发这条语音、图片或文件吧。');
      }
      const token = (await read(config.tokenFile, 'utf8')).trim();
      if (!/^[a-f0-9]{64}$/.test(token)) throw new Error('invalid-token-file');
      const response = await fetchLocal(`${config.backendUrl}/api/internal/clawbot/${audio ? 'voice' : material ? 'materials' : 'chat'}`, {
        method: 'POST', headers: { 'content-type': 'application/json', authorization: `Bearer ${token}` },
        body: JSON.stringify(material ? { account_id, sender_id, message_id, ...material } : audio ? { account_id, sender_id, message_id, audio_base64:audio } :
          { account_id, sender_id, message_id, content }),
        redirect: 'error', signal: ctx.signal
          ? AbortSignal.any([ctx.signal, AbortSignal.timeout(65000)]) : AbortSignal.timeout(65000),
      });
      if (!response.ok) {
        logger.warn(`Xiaoman bridge HTTP ${response.status}; no retry or model fallback.`);
        if (response.status === 429) return handled('消息发送较频繁，请等一分钟再试。此次没有执行新操作。');
        if (audio && [422, 503].includes(response.status)) return handled('语音识别未成功或尚未配置，未执行操作，请发文字重试。');
        if (material && [413, 422, 503].includes(response.status)) {
          // Only relay our local backend's short Chinese material diagnostics.
          let detail;
          const errorReader = response.body?.getReader();
          if (errorReader) {
            const parts = []; let size = 0;
            try {
              while (true) {
                const {done, value} = await errorReader.read(); if (done) break;
                size += value.length; if (size > 2048) break; parts.push(value);
              }
              if (size <= 2048) detail = JSON.parse(Buffer.concat(parts).toString('utf8')).detail;
            } catch {} finally {await errorReader.cancel();}
          }
          return handled(typeof detail === 'string' && detail.length <= 160 && /^[\u4e00-\u9fffA-Za-z0-9\s，。；：、（）\/-]+$/.test(detail)
            ? detail + '；本次未创建提醒。' : '图片或文件识别未成功，请换清晰图片或支持的文件格式。未创建提醒。');
        }
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
      logger.info('Xiaoman bridge handled a trusted Weixin message through the Python service.');
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
    const mediaStore = createMediaStore();
    let textQueue;
    let batchPump;
    let queueError = false;
    const directHandler = createBridgeHandler(api.pluginConfig, { logger: api.logger, mediaStore });
    api.on('message_received', (event, ctx) => mediaStore.observe(event, ctx), { priority: 100 });
    api.on('before_dispatch', (event, ctx) => api.pluginConfig.mergeTextMessages === true
      && (queueError || !textQueue) && ctx.channelId === 'openclaw-weixin'
      ? { handled: true, text: failure }
      : createBridgeHandler(api.pluginConfig, { logger: api.logger, mediaStore, textQueue })(event, ctx),
      { priority: 100, timeoutMs: 75000 });
    api.registerService({
      id: 'xiaoman-reminders', apiVersion: 2,
      start(ctx) {
        // CLI commands also register plugins. Only the actual Gateway service
        // may open the spool and classify interrupted batches after a restart.
        if (api.pluginConfig.mergeTextMessages === true) {
          if (textQueue) throw new Error('batch-service-already-started');
          try {
            textQueue = createTextBatchQueue(path.join(path.dirname(api.pluginConfig.tokenFile), 'text-batches.sqlite'));
            queueError = false;
          } catch { queueError = true; ctx.logger.warn('Xiaoman text spool unavailable; inbound fails closed.'); }
        }
        const pump = createReminderPump(api.pluginConfig, {
          logger: ctx.logger, hostConfig: ctx.config,
          loadAdapter: id => api.runtime.channel.outbound.loadAdapter(id),
        });
        ctx.scheduler.schedule({ id: 'deliver-reminders', delayMs: 0, everyMs: 5000,
          run: () => pump.run(ctx.scheduler.signal) });
        const welcomePump = createWelcomePump(api.pluginConfig, {
          logger: ctx.logger, hostConfig: ctx.config,
          loadAdapter: id => api.runtime.channel.outbound.loadAdapter(id),
        });
        ctx.scheduler.schedule({ id: 'welcome-new-members', delayMs: 1500, everyMs: 5000,
          run: () => welcomePump.run(ctx.scheduler.signal) });
        const stickerPump = createStickerPump(api.pluginConfig, {
          logger: ctx.logger, hostConfig: ctx.config,
          loadAdapter: id => api.runtime.channel.outbound.loadAdapter(id),
        });
        ctx.scheduler.schedule({ id: 'deliver-stickers', delayMs: 2500, everyMs: 5000,
          run: () => stickerPump.run(ctx.scheduler.signal) });
        const announcementPump = createAnnouncementPump(api.pluginConfig, {
          logger: ctx.logger, hostConfig: ctx.config,
          loadAdapter: id => api.runtime.channel.outbound.loadAdapter(id),
        });
        ctx.scheduler.schedule({ id: 'release-announcements', delayMs: 3500, everyMs: 5000,
          run: () => announcementPump.run(ctx.scheduler.signal) });
        if (textQueue) {
          batchPump = createTextBatchPump(api.pluginConfig, { queue: textQueue, handle: directHandler,
            logger: ctx.logger, hostConfig: ctx.config,
            loadAdapter: id => api.runtime.channel.outbound.loadAdapter(id) });
          // Separate scheduler slots permit four users concurrently; queue.claim
          // serializes each user's batches without blocking admission of new text.
          for (let slot = 0; slot < 4; slot++) ctx.scheduler.schedule({ id: `text-batches-${slot}`,
            delayMs: slot * 200, everyMs: 1000, run: () => batchPump.run(ctx.scheduler.signal) });
        }
      },
      async stop() {
        queueError = true; // Stop admitting before draining in-flight work.
        await batchPump?.stop();
        textQueue?.close();
        textQueue = undefined;
        batchPump = undefined;
      },
    });
  },
};
