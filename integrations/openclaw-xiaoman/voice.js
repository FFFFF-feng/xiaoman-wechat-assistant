/** Trusted hook facts only. No URL fetching, body/path parsing or arbitrary file access. */
import fs from 'node:fs/promises';
import {constants} from 'node:fs';
import path from 'node:path';

export const MAX_AUDIO_BYTES = 4 * 1024 * 1024;
const identity = value => typeof value === 'string' && /^[^\x00-\x20\x7f]{1,160}$/.test(value);
const keyFor = (account, sender, message) => JSON.stringify([account, sender, message]);

export function createMediaStore({now = Date.now} = {}) {
  const entries = new Map();
  function expire() {
    for (const [key, value] of entries) if (now() - value.at > 90000) entries.delete(key);
  }
  return {
    observe(event, ctx) {
      if (ctx.channelId !== 'openclaw-weixin') return;
      const sender = ctx.conversationId ?? ctx.senderId ?? event.senderId ?? event.from;
      const account = ctx.accountId, message = ctx.messageId ?? event.messageId;
      if (![account, sender, message].every(identity) ||
          [event.from, event.senderId, ctx.senderId].some(value => value && value !== sender)) return;
      expire();
      if (entries.size >= 500) entries.delete(entries.keys().next().value);
      if (!event.media?.length && !event.originalMedia?.length && !event.mediaStagingPending) return;
      entries.set(keyFor(account, sender, message), {at:now(), content:event.content,
        pending:!!event.mediaStagingPending, quoted:!!event.replyToIsQuote,
        media:(event.media ?? []).slice(0, 3).map(item => ({path:item.path, kind:item.kind,
          contentType:item.contentType, url:item.url}))});
    },
    get(account, sender, message) {expire(); return entries.get(keyFor(account, sender, message));},
  };
}

export async function readVoiceData(config, fact) {
  if (fact?.contentType !== 'audio/wav' || (fact.kind && fact.kind !== 'audio') ||
      typeof fact.path !== 'string' || !path.isAbsolute(fact.path) || fact.url && fact.url !== fact.path) {
    throw new Error('unsupported-voice');
  }
  // Pinned Weixin 2.4.10 saves decrypted media under OPENCLAW_STATE_DIR/media/inbound.
  // The binding file is under that same isolated state's xiaoman directory.
  const root = path.resolve(path.dirname(config.bindingsFile), '..', 'media', 'inbound');
  const rootStat = await fs.lstat(root), givenStat = await fs.lstat(fact.path);
  if (rootStat.isSymbolicLink() || givenStat.isSymbolicLink() || !givenStat.isFile()) throw new Error('invalid-voice-path');
  const [rootReal, fileReal] = await Promise.all([fs.realpath(root), fs.realpath(fact.path)]);
  const relative = path.relative(rootReal, fileReal);
  if (!relative || relative.startsWith('..' + path.sep) || relative === '..' || path.isAbsolute(relative)) {
    throw new Error('invalid-voice-path');
  }
  const file = await fs.open(fileReal, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.size < 44 || stat.size > MAX_AUDIO_BYTES) throw new Error('invalid-voice-size');
    const data = Buffer.alloc(stat.size);
    let offset = 0;
    while (offset < data.length) {
      const {bytesRead} = await file.read(data, offset, data.length - offset, offset);
      if (!bytesRead) throw new Error('incomplete-voice');
      offset += bytesRead;
    }
    if ((await file.stat()).size !== stat.size || data.toString('ascii', 0, 4) !== 'RIFF' ||
        data.toString('ascii', 8, 12) !== 'WAVE') throw new Error('invalid-voice');
    return data.toString('base64');
  } finally {await file.close();}
}
