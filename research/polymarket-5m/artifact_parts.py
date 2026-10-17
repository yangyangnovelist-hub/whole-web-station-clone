"""Offline opaque archive transport. No network, credentials, trading or extraction.

Each part is a separate ZIP_STORED artifact below the connector byte limit.
Publishing every part in ONE Actions artifact defeats this size bound.
Hashes establish byte identity, not market coverage or executable observations.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import zipfile

LIMIT = 512 * 1024**2
DEFAULT_PART = 400 * 1024**2
BLOCK = 1024**2
SCHEMA = 'opaque-research-artifact-parts-v1'


def digest_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(BLOCK), b''):
            h.update(block)
    return h.hexdigest()


def fingerprint(st):
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def split_archive(source, output, part_bytes=DEFAULT_PART):
    source, output = Path(source), Path(output)
    if source.is_symlink() or not source.is_file():
        raise ValueError('source must be an existing sealed regular file, not a symlink')
    if output.exists() or output.is_symlink():
        raise ValueError('output already exists; refusing overwrite')
    # Reserve >=1 MiB for ZIP headers and outer Actions packaging.
    if type(part_bytes) is not int or not 1 <= part_bytes <= LIMIT - BLOCK:
        raise ValueError('invalid part size; reserve outer archive overhead')
    before = source.stat()
    if before.st_size == 0:
        raise ValueError('empty source archive')
    count = (before.st_size + part_bytes - 1) // part_bytes
    if count > 10000:
        raise ValueError('too many parts')
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.artifact-parts-', dir=output.parent))
    whole = hashlib.sha256()
    parts = []
    try:
        with source.open('rb') as f:
            if fingerprint(os.fstat(f.fileno())) != fingerprint(before):
                raise ValueError('source changed before opening')
            for index in range(count):
                name = f'part-{index:05d}.zip'
                p = stage / name
                remaining = min(part_bytes, before.st_size - index * part_bytes)
                expected = remaining
                chunk = hashlib.sha256()
                info = zipfile.ZipInfo('payload.bin', date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                with zipfile.ZipFile(p, 'w', compression=zipfile.ZIP_STORED) as z:
                    with z.open(info, 'w') as out:
                        while remaining:
                            data = f.read(min(BLOCK, remaining))
                            if not data:
                                raise ValueError('source truncated during split')
                            out.write(data); chunk.update(data); whole.update(data)
                            remaining -= len(data)
                if p.stat().st_size >= LIMIT - BLOCK // 2:
                    raise ValueError('part leaves insufficient outer packaging margin')
                parts.append({'index': index, 'name': name, 'payload_bytes': expected,
                              'payload_sha256': chunk.hexdigest(), 'zip_bytes': p.stat().st_size,
                              'zip_sha256': digest_file(p)})
            if f.read(1) or fingerprint(os.fstat(f.fileno())) != fingerprint(before):
                raise ValueError('source changed during split')
        if fingerprint(source.stat()) != fingerprint(before):
            raise ValueError('source replaced during split')
        manifest = {'schema': SCHEMA, 'source_name': source.name, 'source_bytes': before.st_size,
                    'source_sha256': whole.hexdigest(), 'part_payload_bytes': part_bytes,
                    'connector_limit_bytes': LIMIT, 'parts': parts,
                    'admission': 'opaque_bytes_only_not_market_or_oos_validation',
                    'publish_rule': 'one separate Actions artifact per ZIP; never bundle all parts together'}
        m = stage / 'manifest.json'
        m.write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
        # Trusted manifest digest must be retained separately by the caller.
        (stage / 'manifest.sha256').write_text(digest_file(m) + '  manifest.json\n')
        if output.exists() or output.is_symlink():
            raise ValueError('output appeared during split')
        os.rename(stage, output)
        return manifest
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def read_manifest(path, expected_sha256):
    path = Path(path)
    if not re.fullmatch('[0-9a-f]{64}', expected_sha256 or ''):
        raise ValueError('trusted expected manifest SHA256 is required')
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * BLOCK:
        raise ValueError('invalid manifest file')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('manifest hash mismatch')
    def unique_keys(items):
        result = {}
        for k,v in items:
            if k in result:
                raise ValueError('duplicate manifest key')
            result[k] = v
        return result
    m = json.loads(raw, object_pairs_hook=unique_keys)
    if not isinstance(m, dict) or m.get('schema') != SCHEMA:
        raise ValueError('unsupported manifest schema')
    parts = m.get('parts')
    if not isinstance(parts, list) or not 1 <= len(parts) <= 10000:
        raise ValueError('invalid part count')
    total = 0
    for i,p in enumerate(parts):
        if not isinstance(p, dict) or type(p.get('index')) is not int or p['index'] != i or p.get('name') != f'part-{i:05d}.zip':
            raise ValueError('noncanonical part order/name')
        for field in ('payload_sha256','zip_sha256'):
            if not re.fullmatch('[0-9a-f]{64}', p.get(field, '')):
                raise ValueError('invalid part digest')
        if type(p.get('payload_bytes')) is not int or not 0 < p['payload_bytes'] <= LIMIT - BLOCK:
            raise ValueError('invalid payload size')
        if type(p.get('zip_bytes')) is not int or not p['payload_bytes'] <= p['zip_bytes'] < LIMIT:
            raise ValueError('invalid ZIP size')
        total += p['payload_bytes']
    if type(m.get('source_bytes')) is not int or total != m['source_bytes']:
        raise ValueError('source size mismatch')
    if not re.fullmatch('[0-9a-f]{64}', m.get('source_sha256', '')):
        raise ValueError('invalid source digest')
    return m


def assemble(manifest_path, parts_dir, output, expected_manifest_sha256):
    m = read_manifest(manifest_path, expected_manifest_sha256)
    parts_dir, output = Path(parts_dir), Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError('output already exists; refusing overwrite')
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.reassembly-', dir=output.parent)
    stage = Path(name)
    whole = hashlib.sha256()
    try:
        with os.fdopen(fd, 'wb') as dest:
            for p in m['parts']:
                path = parts_dir / p['name']
                if path.is_symlink() or not path.is_file() or path.stat().st_size != p['zip_bytes']:
                    raise ValueError('missing/invalid part: ' + p['name'])
                # Keep one open descriptor for hashing AND decoding.
                with path.open('rb') as raw:
                    h = hashlib.sha256()
                    for b in iter(lambda: raw.read(BLOCK), b''): h.update(b)
                    if h.hexdigest() != p['zip_sha256']:
                        raise ValueError('part ZIP hash mismatch: ' + p['name'])
                    raw.seek(0)
                    with zipfile.ZipFile(raw) as z:
                        entries = z.infolist()
                        if len(entries) != 1 or entries[0].filename != 'payload.bin':
                            raise ValueError('unexpected ZIP members')
                        info = entries[0]
                        if info.file_size != p['payload_bytes'] or info.compress_type != zipfile.ZIP_STORED:
                            raise ValueError('unexpected compression/size')
                        n = 0; h = hashlib.sha256()
                        with z.open(info) as f:
                            for b in iter(lambda: f.read(BLOCK), b''):
                                n += len(b)
                                if n > p['payload_bytes']: raise ValueError('expanded part too large')
                                h.update(b); whole.update(b); dest.write(b)
                        if n != p['payload_bytes'] or h.hexdigest() != p['payload_sha256']:
                            raise ValueError('payload hash mismatch')
            dest.flush(); os.fsync(dest.fileno())
        if stage.stat().st_size != m['source_bytes'] or whole.hexdigest() != m['source_sha256']:
            raise ValueError('reconstructed source hash mismatch')
        # Hard-link publication gives exclusive, atomic creation, unlike replace().
        os.link(stage, output)
        return {'status': 'verified_byte_identical', 'parts': len(m['parts']),
                'bytes': m['source_bytes'], 'sha256': whole.hexdigest(),
                'economic_replay_performed': False, 'formal_oos_result': None}
    finally:
        stage.unlink(missing_ok=True)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    s = sub.add_parser('split');s.add_argument('--source',required=True);s.add_argument('--out',required=True)
    s.add_argument('--part-mib',type=int,default=400)
    a = sub.add_parser('assemble');a.add_argument('--manifest',required=True);a.add_argument('--parts-dir',required=True)
    a.add_argument('--out',required=True);a.add_argument('--expected-manifest-sha256',required=True)
    args = p.parse_args(argv)
    try:
        if args.command == 'split':
            m = split_archive(args.source,args.out,args.part_mib*BLOCK)
            print(json.dumps({'parts':len(m['parts']),'source_sha256':m['source_sha256'],
                              'manifest_sha256':digest_file(Path(args.out)/'manifest.json')}))
        else:
            print(json.dumps(assemble(args.manifest,args.parts_dir,args.out,args.expected_manifest_sha256)))
        return 0
    except (OSError,ValueError,KeyError,zipfile.BadZipFile) as e:
        print(json.dumps({'status':'blocked','reason':str(e),'pnl':None}));return 2


if __name__ == '__main__':
    raise SystemExit(main())
