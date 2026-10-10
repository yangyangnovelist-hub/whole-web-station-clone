"""Run the predeclared 500-rule development screen; never submit live orders."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import mine500_catalog as catalog
import mine500_replay as replay
import mine500_statistics as statistics


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    allow_nan=False) + '\n', encoding='utf-8')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--sizes', default='5,10,20')
    p.add_argument('--latencies', default='500,600,1000')
    args = p.parse_args()
    if not args.input.is_file():
        p.error('real normalized input is required; no empty substitute is run')
    sizes = tuple(float(x) for x in args.sizes.split(','))
    if len(set(sizes)) != len(sizes):
        p.error('duplicate sizes')
    latencies = tuple(int(x) for x in args.latencies.split(','))
    configs = [replay.ReplayConfig(latencies, size, 1000, 250) for size in sizes]
    candidates = catalog.make_candidates() + catalog.make_supplementary_candidates()
    if len(candidates) != 720:
        raise ValueError('expected 500 original + 220 supplementary registered candidates')
    # Do not overwrite evidence from an earlier invocation.
    args.out.mkdir(parents=True, exist_ok=False)
    source_root = Path(__file__).resolve().parent
    sources = {name: digest(source_root / name) for name in (
        'mine500_catalog.py', 'mine500_adapter.py', 'mine500_replay.py',
        'mine500_statistics.py', 'mine500_batch.py', 'h_replay_archive.py')}
    freeze = {
        'schema': 'mine500.development-run-registration.v1',
        'registered_at_utc': datetime.now(timezone.utc).isoformat(),
        'input_name': args.input.name, 'input_sha256': digest(args.input),
        'source_sha256': sources, 'candidate_count': len(candidates),
        'coverage_goal': '500 feature-supported candidates; retain all 220 blocked original candidates',
        'catalog_fingerprint': replay.fingerprint(candidates),
        'configs': [asdict(c) for c in configs],
        'primary': {'shares': 5, 'latency_ms': 500},
        'other_sizes_and_latencies': 'counterfactual stress only; never sum PnL',
        'new_independent_OOS': False,
        'warning': 'Registration before this run is not a prospective holdout: historical data are development.'}
    write_json(args.out / 'registration.json', freeze)
    write_json(args.out / 'catalog.json', candidates)
    for config in configs:
        if sources != {name: digest(source_root / name) for name in sources}:
            raise ValueError('source changed after registration; abort')
        folder = args.out / f'oct9_size{config.shares:g}'
        folder.mkdir()
        result = replay.replay_events(replay.read_jsonl(args.input), candidates,
                                      catalog.signal, config)
        result['candidate_count'] = len(candidates)
        result['registration_file'] = '../registration.json'
        result['signal_code_sha256_actual'] = sources['mine500_catalog.py']
        # This runner is a development screen even if a supplied header overclaims OOS.
        result['split_validation']['effective_split'] = 'development'
        for row in result['summary']:
            row['effective_split'] = 'development'
        write_json(folder / 'replay.json', result)
        report = statistics.analyze(result, family_tests=len(candidates))
        write_json(folder / 'statistics.json', report)
        (folder / 'statistics.md').write_text(statistics.markdown_report(report), encoding='utf-8')
        print(json.dumps({'shares': config.shares, 'candidate_runs': len(result['summary']),
                          'attempt_rows': len(result['attempts']), 'output': str(folder)},
                         ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
