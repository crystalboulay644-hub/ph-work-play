"""Build the static site from a bounded, hash-verified release manifest."""
import concurrent.futures
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import tempfile

source, expected = sys.argv[1:]
if not re.fullmatch(r'https://[a-z0-9-]+\.trycloudflare\.com/releases/classic-v(?:21|22|23)-[a-f0-9]{12}/', source):
    raise ValueError('Invalid release source')
if not re.fullmatch('[a-f0-9]{64}', expected):
    raise ValueError('Invalid manifest checksum')
root = pathlib.Path('site')
root.mkdir(exist_ok=True)

def get(path, target, byte_range=None):
    command = ['curl', '--fail', '--silent', '--show-error', '--location',
                    '--retry', '4', '--retry-all-errors', '--connect-timeout', '15',
                    '--max-time', '150', source + path, '--output', str(target)]
    if byte_range is not None:
        command += ['--range', f'{byte_range[0]}-{byte_range[1]}']
    subprocess.run(command, check=True)

with tempfile.TemporaryDirectory() as temporary:
    manifest = pathlib.Path(temporary) / 'release.json'
    get('release.json', manifest)
    body = manifest.read_bytes()
    if hashlib.sha256(body).hexdigest() != expected:
        raise ValueError('Release manifest checksum differs')
    rows = json.loads(body)['files']
    if not 1 <= len(rows) <= 5000 or sum(row['bytes'] for row in rows) > 950_000_000:
        raise ValueError('Invalid release size')
    seen = set()
    for row in rows:
        path = pathlib.PurePosixPath(row['path'])
        if path.is_absolute() or '..' in path.parts or row['path'] in seen:
            raise ValueError('Invalid release path')
        seen.add(row['path'])

    def download(row):
        target = root / row['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and not target.is_symlink():
            value = target.read_bytes()
            if len(value) == row['bytes'] and hashlib.sha256(value).hexdigest() == row['sha256']:
                return 'reused ' + row['path']
        for attempt in range(3):
            if row['bytes'] > 8 * 1024 * 1024:
                with target.open('wb') as output:
                    for offset in range(0, row['bytes'], 8 * 1024 * 1024):
                        end = min(row['bytes'], offset + 8 * 1024 * 1024) - 1
                        part = target.with_suffix(target.suffix + '.part')
                        get(row['path'], part, (offset, end))
                        chunk = part.read_bytes()
                        if len(chunk) != end - offset + 1:
                            raise ValueError('Unexpected range length: ' + row['path'])
                        output.write(chunk)
                        part.unlink()
                        print(f"range {row['path']} {offset}-{end} verified", flush=True)
            else:
                get(row['path'], target)
            value = target.read_bytes()
            if len(value) == row['bytes'] and hashlib.sha256(value).hexdigest() == row['sha256']:
                return row['path']
        raise ValueError('Release file checksum differs: ' + row['path'])

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        for index, path in enumerate(executor.map(download, rows), 1):
            print(f'{index}/{len(rows)} verified {path}', flush=True)
    for path in root.rglob('*'):
        if path.is_file() and path.relative_to(root).as_posix() not in seen:
            path.unlink()
