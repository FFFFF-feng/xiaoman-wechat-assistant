"""Fetch the reviewed, pinned Fluent PNG list; verify original Git blob hashes."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import re
from urllib.parse import quote
from urllib.request import Request, urlopen

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'assets/stickers/fluent-source.json'
ASSETS = ROOT / 'assets/stickers/phoenix-d'
MAX_BYTES = 4 * 1024 * 1024


def download(commit, path, expected):
    url = f'https://raw.githubusercontent.com/microsoft/fluentui-emoji/{commit}/' + quote(path, safe='/')
    request = Request(url, headers={'User-Agent': 'xiaoman-reviewed-sticker-fetch'})
    with urlopen(request, timeout=45) as response:
        if response.url != url:
            raise ValueError('unexpected redirect')
        data = response.read(MAX_BYTES + 1)
    blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    if len(data) > MAX_BYTES or blob != expected:
        raise ValueError('source Git blob mismatch')
    return data, url


def main():
    config = json.loads(CONFIG.read_text(encoding='utf-8'))
    commit = config['commit']
    if config['repository'] != 'https://github.com/microsoft/fluentui-emoji' or not re.fullmatch('[a-f0-9]{40}', commit):
        raise ValueError('unreviewed repository or ref')
    license_data, _ = download(commit, 'LICENSE', config['license_git_blob_sha'])
    if not license_data.decode('utf-8-sig').lstrip().startswith('MIT License'):
        raise ValueError('unexpected license')
    results = []

    def fetch(entry):
        if not re.fullmatch(r'fluent_[a-z_]+', entry['id']) or not entry['path'].startswith('assets/') or not entry['path'].endswith('.png') or '..' in entry['path']:
            raise ValueError('unreviewed asset path')
        data, url = download(commit, entry['path'], entry['git_blob_sha'])
        with Image.open(io.BytesIO(data)) as picture:
            if picture.format != 'PNG' or picture.size != (256, 256) or picture.mode != 'RGBA':
                raise ValueError('unexpected image format')
            picture.verify()
        return entry, data, url

    # Do not modify the bundle until all source files and the license verify.
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fetch, config['assets']))
    catalog = json.loads((ASSETS / 'manifest.json').read_text(encoding='utf-8'))
    license_dir = ASSETS / 'licenses'
    license_dir.mkdir(exist_ok=True)
    (license_dir / 'fluent-emoji-LICENSE.txt').write_bytes(license_data)
    for entry, data, url in results:
        (ASSETS / (entry['id'] + '.png')).write_bytes(data)
        catalog[entry['id']] = {'file': entry['id'] + '.png', 'caption': entry['caption'],
            'sha256': hashlib.sha256(data).hexdigest(), 'style': 'fluent', 'scenes': entry['scenes'],
            'source_url': url, 'source_git_blob': entry['git_blob_sha'],
            'license': 'MIT', 'license_file': 'licenses/fluent-emoji-LICENSE.txt'}
    (ASSETS / 'manifest.json').write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'PASS: {len(results)} original PNGs verified; {len(catalog)} bundled images; license retained')


if __name__ == '__main__':
    main()
