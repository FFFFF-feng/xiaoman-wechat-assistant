import { DatabaseSync } from 'node:sqlite';
import { createHash } from 'node:crypto';
import { mkdirSync, lstatSync, chmodSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import path from 'node:path';

const hash = value => createHash('sha256').update(value).digest('hex');
const MAX_MESSAGES = 40, MAX_CONTENT = 8000;

// One Gateway owns this private SQLite spool. Never run a second Gateway against
// the same credentials. Raw chat stays on disk under the private runtime only.
export function createTextBatchQueue(filename, { now = Date.now, quietMs = 4000, maxWaitMs = 30000 } = {}) {
  if (filename !== ':memory:') {
    if (!path.isAbsolute(filename)) throw new Error('queue-path-not-absolute');
    const directory = path.dirname(filename);
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    if (lstatSync(directory).isSymbolicLink()) throw new Error('queue-directory-symlink');
    try { if (lstatSync(filename).isSymbolicLink()) throw new Error('queue-file-symlink'); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  const db = new DatabaseSync(filename);
  if (filename !== ':memory:') chmodSync(filename, 0o600);
  try { db.exec(`PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL; PRAGMA locking_mode=EXCLUSIVE;
    CREATE TABLE IF NOT EXISTS batches (
      id TEXT PRIMARY KEY, owner TEXT NOT NULL, status TEXT NOT NULL, created INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS fragments (
      seq INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, owner TEXT NOT NULL,
      account TEXT NOT NULL, sender TEXT NOT NULL, message TEXT NOT NULL,
      content TEXT NOT NULL, received INTEGER NOT NULL, batch TEXT);
    CREATE INDEX IF NOT EXISTS fragments_pending ON fragments(owner, batch, received);`); }
  catch (error) { db.close(); throw error; }
  // A crash after claiming may follow a committed task or an accepted send.
  // Do not replay that batch or invent a successful delivery receipt.
  db.prepare("UPDATE batches SET status='unknown' WHERE status IN ('processing','sending')").run();
  const busy = new Set();
  return {
    enqueue(item) {
      if (![item.account_id, item.sender_id, item.message_id].every(v => typeof v === 'string' && v.length && v.length <= 160 && !/[\x00-\x1f]/.test(v)) ||
          typeof item.content !== 'string' || !item.content.trim() || item.content.length > MAX_CONTENT) throw new Error('invalid-fragment');
      const owner = hash(JSON.stringify([item.account_id, item.sender_id]));
      const key = hash(JSON.stringify([item.account_id, item.sender_id, item.message_id]));
      const existing = db.prepare('SELECT content FROM fragments WHERE key=?').get(key);
      if (existing) {
        if (existing.content !== item.content) throw new Error('message-id-conflict');
        return; // Original message IDs stay deduplicated, including after restart.
      }
      const backlog = db.prepare('SELECT COUNT(*) AS n FROM fragments WHERE batch IS NULL').get().n;
      const personal = db.prepare('SELECT COUNT(*) AS n FROM fragments WHERE owner=? AND batch IS NULL').get(owner).n;
      if (backlog >= 1000 || personal >= 120) throw new Error('queue-full');
      db.prepare('INSERT INTO fragments(key,owner,account,sender,message,content,received) VALUES(?,?,?,?,?,?,?)')
        .run(key, owner, item.account_id, item.sender_id, item.message_id, item.content, now());
    },
    hasWaiting(account, sender) {
      const owner = hash(JSON.stringify([account, sender]));
      return busy.has(owner) || !!db.prepare('SELECT seq FROM fragments WHERE owner=? AND batch IS NULL LIMIT 1').get(owner);
    },
    claim() {
      const owners = db.prepare(`SELECT owner, MIN(received) AS first, MAX(received) AS last,
        COUNT(*) AS n FROM fragments WHERE batch IS NULL GROUP BY owner ORDER BY first`).all();
      for (const group of owners) {
        if (busy.has(group.owner)) continue;
        if (now() - group.last < quietMs && now() - group.first < maxWaitMs && group.n < MAX_MESSAGES) continue;
        const rows = db.prepare('SELECT * FROM fragments WHERE owner=? AND batch IS NULL ORDER BY seq LIMIT ?')
          .all(group.owner, MAX_MESSAGES);
        const chosen = []; let content = '';
        for (const row of rows) {
          const joined = content ? `${content}\n${row.content}` : row.content;
          if (joined.length > MAX_CONTENT) break;
          content = joined; chosen.push(row);
        }
        // The derived ID is based on trusted ordered source IDs, never text.
        const id = `batch-${hash(JSON.stringify(chosen.map(row => row.key)))}`;
        db.exec('BEGIN IMMEDIATE');
        try {
          db.prepare("INSERT INTO batches(id,owner,status,created) VALUES(?,?,'processing',?)").run(id, group.owner, now());
          for (const row of chosen) db.prepare('UPDATE fragments SET batch=? WHERE seq=? AND batch IS NULL').run(id, row.seq);
          db.exec('COMMIT');
        } catch (error) { db.exec('ROLLBACK'); throw error; }
        busy.add(group.owner);
        return { id, owner: group.owner, account_id: chosen[0].account, sender_id: chosen[0].sender,
          message_id: id, content, count: chosen.length };
      }
      return null;
    },
    sending(item) {
      if (db.prepare("UPDATE batches SET status='sending' WHERE id=? AND status='processing'").run(item.id).changes !== 1)
        throw new Error('batch-not-sendable');
    },
    finish(item, status) {
      if (!['accepted', 'failed', 'unknown', 'cancelled'].includes(status)) throw new Error('invalid-outcome');
      db.prepare('UPDATE batches SET status=? WHERE id=?').run(status, item.id);
      busy.delete(item.owner);
    },
    counts() {
      return { pending: db.prepare('SELECT COUNT(*) AS n FROM fragments WHERE batch IS NULL').get().n,
        batches: db.prepare('SELECT status, COUNT(*) AS n FROM batches GROUP BY status').all() };
    },
    close() { db.close(); },
  };
}

export function createTextBatchPump(config, deps) {
  const read = deps.readFile ?? readFile;
  const logger = deps.logger ?? { info() {}, warn() {} };
  let active = 0;
  let stopped = false;
  const drained = [];
  async function authorized(item) {
    const bindings = JSON.parse(await read(config.bindingsFile, 'utf8'));
    return Array.isArray(bindings) && bindings.some(v => v.account_id === item.account_id && v.sender_id === item.sender_id);
  }
  return {
    async run(signal) {
      if (stopped || signal?.aborted || active >= 4) return;
      active++;
      let item;
      try {
        const adapter = await deps.loadAdapter('openclaw-weixin');
        if (typeof adapter?.sendText !== 'function' || signal?.aborted || stopped) return;
        item = deps.queue.claim();
        if (!item) return;
        if (!await authorized(item)) { deps.queue.finish(item, 'cancelled'); return; }
        const response = await deps.handle({ content: item.content }, { channelId: 'openclaw-weixin',
          accountId: item.account_id, senderId: item.sender_id, messageId: item.message_id, signal });
        if (stopped || signal?.aborted || !await authorized(item)) { deps.queue.finish(item, 'cancelled'); return; }
        if (!response?.handled || typeof response.text !== 'string' || !response.text.trim()) throw new Error('empty-batch-reply');
        deps.queue.sending(item); // Commit before side effect; never automatically resend.
        const receipt = await adapter.sendText({ cfg: deps.hostConfig, accountId: item.account_id,
          to: item.sender_id, text: response.text }); // Official adapter owns latest context token.
        const accepted = receipt?.messageId && !['failed', 'unknown', 'cancelled', 'suppressed'].includes(receipt.outcome);
        deps.queue.finish(item, accepted ? 'accepted' : 'unknown');
        logger.info(`Xiaoman text batch processed: fragments=${item.count}, platformAccepted=${!!accepted}.`);
      } catch {
        if (item) deps.queue.finish(item, 'unknown');
        logger.warn('Xiaoman text batch interrupted; no automatic task replay or resend.');
      } finally {
        active--;
        if (!active) for (const resolve of drained.splice(0)) resolve();
      }
    },
    async stop() {
      stopped = true;
      if (active) await new Promise(resolve => drained.push(resolve));
    },
  };
}
