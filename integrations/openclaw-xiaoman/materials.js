import path from 'node:path';
import { readInboundData } from './voice.js';

export const MAX_MATERIAL_BYTES = 10 * 1024 * 1024;
const imageTypes = new Set(['image/jpeg', 'image/png', 'image/webp']);
const formats = new Set(['.txt', '.md', '.csv', '.json', '.xml', '.pdf', '.docx', '.xlsx']);

export async function readMaterialData(config, fact) {
  if (typeof fact?.path !== 'string') throw new Error('missing-local-attachment');
  const kind = imageTypes.has(fact.contentType) ? 'image' : 'file';
  if (fact.kind && (kind === 'image' ? fact.kind !== 'image' : !['file', 'document'].includes(fact.kind))) {
    throw new Error('unsupported-media-kind');
  }
  const savedName = path.basename(fact.path);
  // OpenClaw saves original.ext---uuid.detected-ext. Keep the original format
  // for XML/JSON/CSV when generic MIME detection renamed it to .txt or .bin.
  const sourceName = savedName.match(/^(.+)---[0-9a-f-]{36}(?:\.[A-Za-z0-9]+)?$/i)?.[1];
  const filename = sourceName && formats.has(path.extname(sourceName).toLowerCase()) ? sourceName : savedName;
  if (!filename || filename.length > 200 || /[\x00-\x1f\x7f]/.test(filename) ||
      kind === 'file' && !formats.has(path.extname(filename).toLowerCase())) {
    throw new Error('unsupported-file-format');
  }
  const data = await readInboundData(config, fact, MAX_MATERIAL_BYTES);
  return { kind, filename, data_base64: data.toString('base64') };
}
