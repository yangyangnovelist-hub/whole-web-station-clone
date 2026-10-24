"""Immutable raw capture handoff, made BEFORE conversion or model audits.

Only allowlisted public-market capture files are included. No environment,
credentials or repository .env files are copied. A byte-valid checkpoint is
not a claim of continuous websocket health or a complete gzip event stream.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tarfile
import time

ROOT_FILES = {'ledger.csv', 'report.md', 'capture-runtime.json', 'capture-health.json'}
SCHEMA = 'capture-checkpoint-v1'


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def allowed(name):
    p = PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts:
        return False
    return name in ROOT_FILES or (len(p.parts) == 3 and p.parts[0] == 'raw' and
                                 p.parts[-1].endswith('.jsonl.gz'))


def pack(root, output, source_commit='', capture_outcome='unknown', max_bytes=6*1024**3):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('checkpoint already exists')
    files = sorted(p for p in root.rglob('*') if p.is_file() and allowed(p.relative_to(root).as_posix()))
    if not any(p.relative_to(root).parts[0] == 'raw' for p in files):
        raise ValueError('no raw capture files')
    rows = []
    total = 0
    for p in files:
        if p.is_symlink() or any(x.is_symlink() for x in p.parents if x != root):
            raise ValueError('symlink is not a recording file')
        total += p.stat().st_size
        if total > max_bytes:
            raise ValueError('raw checkpoint exceeds configured byte cap; not silently truncated')
        rows.append(dict(path=p.relative_to(root).as_posix(), bytes=p.stat().st_size, sha256=sha(p)))
    manifest = dict(schema=SCHEMA, created_ms=time.time_ns()/1e6, source_commit=source_commit,
                    run_id=os.environ.get('GITHUB_RUN_ID'), capture_outcome=capture_outcome,
                    complete_feed_health_verified=False, full_gzip_decode_verified=False, files=rows)
    manifest_bytes = (json.dumps(manifest, sort_keys=True) + '\n').encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + '.pending')
    import io
    with tarfile.open(temporary, 'w') as t:
        info = tarfile.TarInfo('capture-manifest.json')
        info.size = len(manifest_bytes)
        t.addfile(info, io.BytesIO(manifest_bytes))
        for row, p in zip(rows, files):
            before = p.stat()
            t.add(p, arcname=row['path'], recursive=False)
            after = p.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('capture changed while checkpointing; stop writer first')
    temporary.replace(output)
    digest = sha(output)
    output.with_suffix(output.suffix + '.sha256').write_text(digest + '\n')
    return dict(path=str(output), sha256=digest, files=len(rows), bytes=total,
                capture_outcome=capture_outcome, complete_feed_health_verified=False)


def restore(archive, destination, max_bytes=6*1024**3):
    archive, destination = Path(archive).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError('restore destination already exists')
    expected = archive.with_suffix(archive.suffix + '.sha256').read_text().strip()
    if len(expected) != 64 or sha(archive) != expected:
        raise ValueError('checkpoint archive hash mismatch')
    with tarfile.open(archive, 'r:') as t:
        members = t.getmembers()
        if len({m.name for m in members}) != len(members) or not all(m.isfile() for m in members):
            raise ValueError('duplicate or nonregular checkpoint members')
        if not members or members[0].name != 'capture-manifest.json' or members[0].size > 4*1024**2:
            raise ValueError('missing or excessive checkpoint manifest')
        manifest = json.load(t.extractfile(members[0]))
        if manifest.get('schema') != SCHEMA:
            raise ValueError('unknown checkpoint schema')
        rows = manifest['files']
        if len({row['path'] for row in rows}) != len(rows):
            raise ValueError('duplicate manifest paths')
        if {r['path'] for r in rows} != {m.name for m in members[1:]}:
            raise ValueError('checkpoint inventory mismatch')
        if not all(allowed(row['path']) for row in rows) or sum(m.size for m in members) > max_bytes + 4*1024**2:
            raise ValueError('unsafe or oversized checkpoint')
        by_name = {m.name:m for m in members}
        destination.mkdir(parents=True, exist_ok=False)
        for row in rows:
            m = by_name[row['path']]
            if row['bytes'] != m.size:
                raise ValueError('checkpoint size mismatch')
            path = destination / row['path']
            path.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with t.extractfile(m) as src, path.open('xb') as dst:
                while data := src.read(1024*1024):
                    dst.write(data); digest.update(data)
            if digest.hexdigest() != row['sha256']:
                raise ValueError('checkpoint member hash mismatch')
        (destination / 'capture-manifest.json').write_text(json.dumps(manifest, sort_keys=True) + '\n')
    return dict(restored_files=len(rows), capture_outcome=manifest['capture_outcome'],
                source_commit=manifest['source_commit'], complete_feed_health_verified=False)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    a = sub.add_parser('pack'); a.add_argument('--root', required=True); a.add_argument('--out', required=True)
    a.add_argument('--source-commit', default=''); a.add_argument('--capture-outcome', default='unknown')
    a = sub.add_parser('restore'); a.add_argument('--archive', required=True); a.add_argument('--out', required=True)
    args = p.parse_args()
    result = pack(args.root, args.out, args.source_commit, args.capture_outcome) if args.command == 'pack' else restore(args.archive, args.out)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
