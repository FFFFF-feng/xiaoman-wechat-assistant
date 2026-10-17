import { readFile } from 'node:fs/promises';
import { resolveSticker } from './sticker-assets.js';

// Runs inside the Gateway. The installed Tencent channel owns credentials,
// context tokens and its provider API; this worker never reads or calls iLink.
export function createReminderPump(config, deps) {
  return createOutboxPump(config, deps, '/outbox');
}

export function createWelcomePump(config, deps) {
  return createOutboxPump(config, deps, '/welcome');
}

export function createAnnouncementPump(config, deps) {
  return createOutboxPump(config, deps, '/announcements');
}

export function createStickerPump(config, deps) {
  return createOutboxPump(config, deps, '/stickers', true);
}

function createOutboxPump(config, deps, prefix, media = false) {
  const read = deps.readFile ?? readFile;
  const fetchLocal = deps.fetch ?? globalThis.fetch;
  const logger = deps.logger ?? { info() {}, warn() {} };
  let active = false;
  async function post(path, body, signal) {
    if (config.backendUrl !== 'http://127.0.0.1:18083') throw new Error('invalid-backend');
    const token = (await read(config.tokenFile, 'utf8')).trim();
    if (!/^[a-f0-9]{64}$/.test(token)) throw new Error('invalid-token-file');
    const response = await fetchLocal(`${config.backendUrl}/api/internal/clawbot${path}`, {
      method: 'POST', headers: { 'content-type': 'application/json', authorization: `Bearer ${token}` },
      body: JSON.stringify(body ?? {}), redirect: 'error',
      signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(10000)]) : AbortSignal.timeout(10000),
    });
    if (!response.ok) {
      const error = new Error('local-outbox-rejected');
      error.status = response.status;
      throw error;
    }
    const raw = await response.text();
    if (raw.length > 16000) throw new Error('outbox-response-too-large');
    return JSON.parse(raw);
  }
  return {
    async run(signal) {
      if (active || signal?.aborted) return;
      active = true;
      let item;
      let sending = false;
      let result;
      try {
        const adapter = await deps.loadAdapter('openclaw-weixin');
        if (typeof adapter?.[media ? 'sendMedia' : 'sendText'] !== 'function') throw new Error('channel-adapter-unavailable');
        item = (await post(`${prefix}/claim`, {}, signal)).delivery;
        if (!item) return;
        const bindings = JSON.parse(await read(config.bindingsFile, 'utf8'));
        const allowed = Array.isArray(bindings) && bindings.some(v => v.account_id === item.account_id && v.sender_id === item.sender_id);
        if (!allowed || typeof item.text !== 'string' || (!media && !item.text) || item.text.length > 4000 || signal?.aborted) {
          // No send has begun; the claim will expire or be cancelled by the backend.
          logger.warn('Xiaoman reminder claim not sent: binding/content/service checks failed.');
          return;
        }
        let asset = null;
        if (media) {
          try { asset = await (deps.resolveSticker ?? resolveSticker)(item.sticker_id, config.stickerAssetsDir); }
          catch {
            await post(`${prefix}/${item.id}/result`, { claim_token: item.claim_token,
              outcome: 'failed', error_code: 'asset_unavailable' });
            logger.warn(`Xiaoman ${prefix} delivery ${item.id}: fixed asset unavailable before send.`);
            return;
          }
        }
        await post(`${prefix}/${item.id}/start`, { claim_token: item.claim_token }, signal);
        sending = true;
        if (signal?.aborted) {
          result = { outcome: 'failed', error_code: 'service_stopped_before_send' };
        } else {
          try {
            const target = { cfg: deps.hostConfig, to: item.sender_id, text: item.text, accountId: item.account_id };
            const receipt = media
              ? await adapter.sendMedia({ ...target, text: '', mediaUrl: asset.file, mediaLocalRoots: [asset.root] })
              : await adapter.sendText(target);
            result = receipt?.messageId && !['failed', 'cancelled', 'suppressed', 'unknown'].includes(receipt.outcome)
              ? { outcome: 'accepted', message_id: String(receipt.messageId).slice(0, 100) }
              : { outcome: 'failed', error_code: 'no_platform_acceptance' };
          } catch {
            // A timeout can follow platform acceptance. Do not auto-send again.
            result = { outcome: 'unknown', error_code: 'send_result_unknown' };
          }
        }
        await post(`${prefix}/${item.id}/result`, { claim_token: item.claim_token, ...result });
        logger.info(`Xiaoman ${prefix} delivery ${item.id}: ${result.outcome}.`);
      } catch (error) {
        // Do not print message bodies, targets, claim tokens or provider errors.
        if (item && sending) logger.warn(`Xiaoman reminder delivery ${item.id}: result not recorded; no resend.`);
        else if (error.status !== 503 && error.status !== 409 && !signal?.aborted) logger.warn('Xiaoman reminder worker temporarily unavailable.');
      } finally { active = false; }
    },
  };
}
