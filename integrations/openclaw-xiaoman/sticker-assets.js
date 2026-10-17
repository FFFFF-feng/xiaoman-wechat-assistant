import { readFile, realpath, lstat } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = fileURLToPath(new URL('../../assets/stickers/phoenix-d/', import.meta.url));
const IDS = new Set(['morning', 'received', 'cheer', 'rest', 'thanks', 'goodnight',
  'laugh', 'hug', 'here', 'sad', 'okay', 'comfort', 'wait', 'go']);

export async function resolveSticker(id, root = ROOT) {
  if (!IDS.has(id)) throw new Error('unlisted-sticker');
  const resolvedRoot = await realpath(root);
  if ((await lstat(root)).isSymbolicLink() || path.resolve(root) !== resolvedRoot) throw new Error('unsafe-sticker-root');
  const catalog = JSON.parse(await readFile(path.join(root, 'manifest.json'), 'utf8'));
  const entry = catalog[id];
  if (!entry || entry.file !== `${id}.png` || !/^[a-f0-9]{64}$/.test(entry.sha256)) throw new Error('invalid-sticker-manifest');
  const file = path.join(resolvedRoot, entry.file);
  const info = await lstat(file);
  if (!info.isFile() || info.isSymbolicLink() || info.size > 4*1024*1024
      || path.dirname(await realpath(file)) !== resolvedRoot) throw new Error('unsafe-sticker-file');
  const data = await readFile(file);
  if (!data.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10]))
      || createHash('sha256').update(data).digest('hex') !== entry.sha256) throw new Error('sticker-integrity-failed');
  return { file, root: resolvedRoot };
}
