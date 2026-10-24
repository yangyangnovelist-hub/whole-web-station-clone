"""Reconstruct a completely published research archive without network or trading.

The expected manifest digest must come from the trusted producing job output,
not an unchecked downloaded manifest.sha256. No economic admission is implied.
"""
import argparse
import json
from pathlib import Path
import re
from artifact_parts import assemble, read_manifest


def restore(index_dir, parts_dir, output, expected_manifest_sha256, expected_prefix, expected_run_id):
    index_dir, parts_dir, output = Path(index_dir), Path(parts_dir), Path(output)
    m = read_manifest(index_dir/'manifest.json', expected_manifest_sha256)
    if output.exists() or output.is_symlink() or output.with_suffix(output.suffix+'.sha256').exists():
        raise ValueError('restore output/sidecar already exists')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,180}', expected_prefix):
        raise ValueError('invalid expected prefix')
    # Publication index records each upload's completed status and artifact ID.
    raw = index_dir/'publish-index.json'
    if raw.is_symlink() or raw.stat().st_size>1024**2:raise ValueError('invalid publish index')
    idx=json.loads(raw.read_text())
    if idx.get('schema')!='research-archive-publication-v1' or idx.get('status')!='all_parts_uploaded':
        raise ValueError('incomplete publication')
    if str(idx.get('run_id')) != str(expected_run_id) or idx.get('prefix')!=expected_prefix:
        raise ValueError('publication origin mismatch')
    if idx.get('manifest_sha256') != expected_manifest_sha256:
        raise ValueError('publication manifest mismatch')
    rows=idx.get('parts')
    if not isinstance(rows,list) or len(rows)!=len(m['parts']):raise ValueError('publication part count mismatch')
    seen=set()
    for p,r in zip(m['parts'],rows):
        if r.get('name')!=p['name'] or r.get('artifact_name')!=f"{expected_prefix}-part-{p['index']:05d}":
            raise ValueError('publication part identity mismatch')
        aid=str(r.get('artifact_id',''))
        if not aid.isdigit() or aid=='0' or aid in seen:raise ValueError('invalid/reused artifact ID')
        seen.add(aid)
    result=assemble(index_dir/'manifest.json',parts_dir,output,expected_manifest_sha256)
    # Existing capture_checkpoint restore expects this sidecar. Its value comes
    # only from the hash-verified manifest, never from an untrusted companion.
    with output.with_suffix(output.suffix+'.sha256').open('x') as f:f.write(m['source_sha256']+'\n')
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('index-dir','parts-dir','out','expected-manifest-sha256','expected-prefix','expected-run-id'):
        p.add_argument('--'+name,required=True)
    a=p.parse_args()
    try:
        print(json.dumps(restore(a.index_dir,a.parts_dir,a.out,a.expected_manifest_sha256,a.expected_prefix,a.expected_run_id)))
    except (OSError,ValueError,KeyError,TypeError) as e:
        print(json.dumps({'status':'blocked','reason':str(e),'pnl':None}));return 2
    return 0


if __name__=='__main__':raise SystemExit(main())
