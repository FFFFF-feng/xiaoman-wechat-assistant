import { readFile, realpath, lstat } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = fileURLToPath(new URL('../../assets/stickers/phoenix-d/', import.meta.url));
const IDS = new Set(['morning', 'received', 'cheer', 'rest', 'thanks', 'goodnight',
  'laugh', 'hug', 'here', 'sad', 'okay', 'comfort', 'wait', 'go',
  'fluent_cat_hello', 'fluent_cat_laugh', 'fluent_cat_cry', 'fluent_cat_love',
  'fluent_hug', 'fluent_tears', 'fluent_plead', 'fluent_party',
  'fluent_salute', 'fluent_thumb', 'fluent_clap', 'fluent_thanks',
  'fluent_cheer', 'fluent_wave', 'fluent_relax', 'fluent_peek',
  'fluent_sleep', 'fluent_cool', 'fluent_heart', 'fluent_dog',
  'fluent_rabbit', 'fluent_panda', 'fluent_coffee', 'fluent_laugh']);

export async function resolveSticker(id, root = ROOT) {
  if (!IDS.has(id)) throw new Error('unlisted-sticker');
  if (typeof root !== 'string' || !path.isAbsolute(root)) throw new Error('unsafe-sticker-root');
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
