"""Post-development, fixed-attempt missed-fill sensitivity; never an OOS replay.

Delete the most profitable filled orders with hindsight. Keep losing fills and
all original failed attempts. This is an adversarial sensitivity envelope, NOT
an estimated venue fill probability, a tradable selector, or a new cash replay.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

FRACTIONS = (0.0, 0.10, 0.25, 0.50)
EXTRA_COSTS = (0.0, 0.01)
EPS = 1e-9


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    allow_nan=False) + "\n")


def sensitivity(fills, fraction, extra_cost):
    """Remove up to floor(fraction * filled orders) beneficial contributions.

    Extra cost applies only to retained shares. Deleted partial fills count as
    one order and remove their actual cost, fee, shares and settlement payout.
    """
    if not 0 <= fraction <= 1 or not math.isfinite(extra_cost) or extra_cost < 0:
        raise ValueError("invalid sensitivity configuration")
    contributions = [float(x['net_pnl_usd']) - extra_cost * x['filled_shares']
                     for x in fills]
    if not all(math.isfinite(p) for p in contributions):
        raise ValueError("non-finite contribution")
    ranked = sorted((i for i, p in enumerate(contributions) if p > 0),
                    key=lambda i: (-contributions[i], str(fills[i]['market_id']),
                                   fills[i]['decision_ms']))
    deleted = set(ranked[:math.floor(fraction * len(fills))])
    base = math.fsum(contributions)
    reduced = base
    break_even = 0 if base <= EPS else None
    if break_even is None:
        for count, i in enumerate(ranked, 1):
            reduced -= contributions[i]
            if reduced <= EPS:
                break_even = count
                break
    retained = [x for i, x in enumerate(fills) if i not in deleted]
    cost = math.fsum(x['cost_usd'] for x in retained)
    payout = math.fsum(x['payout_usd'] for x in retained)
    shares = math.fsum(x['filled_shares'] for x in retained)
    net = payout - cost - extra_cost * shares
    expected = base - math.fsum(contributions[i] for i in deleted)
    if not math.isclose(net, expected, abs_tol=1e-7, rel_tol=1e-10):
        raise ValueError("cost/payout reconciliation failed")
    by_market = defaultdict(float)
    for x, p in zip(fills, contributions):
        by_market[x['market_id']] += p
    return {
        'missed_fraction_budget': fraction, 'extra_cost_per_retained_share': extra_cost,
        'original_filled_orders': len(fills), 'original_adjusted_net_pnl_usd': base,
        'missed_positive_orders': len(deleted), 'remaining_filled_orders': len(retained),
        'remaining_shares': shares, 'remaining_cost_with_original_fees_usd': cost,
        'remaining_original_fees_usd': math.fsum(x['fees_usd'] for x in retained),
        'remaining_payout_usd': payout, 'net_pnl_usd': net,
        'positive': net > EPS,
        'minimum_positive_orders_to_erase_profit': break_even,
        'break_even_missed_fraction_of_all_fills': break_even / len(fills) if fills and break_even is not None else None,
        'net_after_worst_single_market_deleted_usd': base - max(0, max(by_market.values(), default=0)),
        'removed_market_ids': [fills[i]['market_id'] for i in sorted(deleted)],
    }


def signature(attempts, filled_only=False):
    fields = ('market_id', 'decision_ms', 'side', 'status', 'limit_price',
              'filled_shares', 'cost_usd', 'fees_usd', 'payout_usd')
    rows = [tuple(x.get(k) for k in fields) for x in attempts
            if not filled_only or x.get('filled_shares', 0) > 0]
    return hashlib.sha256(json.dumps(sorted(rows, key=repr), separators=(',', ':')).encode()).hexdigest()


def analyse_document(doc):
    if doc.get('split_validation', {}).get('effective_split') != 'development':
        raise ValueError('this descriptive tool accepts development ledgers only')
    grouped = defaultdict(list)
    for a in doc['attempts']:
        grouped[a['candidate_id'], a['latency_ms']].append(a)
    rows = []
    for s in doc['summary']:
        attempts = grouped[s['candidate_id'], s['latency_ms']]
        if len(attempts) != s['attempts']:
            raise ValueError('attempt count mismatch')
        fills = [a for a in attempts if a.get('filled_shares', 0) > 0]
        eligible = (s['status'] == 'simulated_complete' and
                    s['unresolved_positions'] == 0 and not s['missing_execution_inputs'])
        if eligible:
            if any(not a.get('settled') for a in fills):
                raise ValueError('unsettled fill in complete summary')
            if not math.isclose(math.fsum(a['net_pnl_usd'] for a in fills),
                                s['net_pnl_usd'], abs_tol=1e-7):
                raise ValueError('base PnL mismatch')
        for extra in EXTRA_COSTS:
            for fraction in FRACTIONS:
                row = {'candidate_id': s['candidate_id'], 'latency_ms': s['latency_ms'],
                       'requested_shares': s['requested_shares'], 'status': s['status'],
                       'attempts': s['attempts'], 'original_submitted': s['submitted_orders'],
                       'original_status_counts': s['status_counts'], 'eligible': eligible,
                       'extra_cost_per_retained_share': extra, 'missed_fraction_budget': fraction,
                       'net_pnl_usd': None, 'positive': False,
                       'attempt_signature': signature(attempts),
                       'filled_signature': signature(attempts, filled_only=True)}
                if eligible:
                    row.update(sensitivity(fills, fraction, extra))
                rows.append(row)
    return rows


def aggregate(rows):
    grouped = defaultdict(list)
    for r in rows:
        grouped[r['requested_shares'], r['latency_ms'], r['extra_cost_per_retained_share'], r['missed_fraction_budget']].append(r)
    result = []
    for key, group in sorted(grouped.items()):
        positives = [r for r in group if r['positive']]
        result.append(dict(zip(('shares', 'latency_ms', 'extra_cost', 'missed_fraction'), key),
                           registered=len(group), eligible=sum(r['eligible'] for r in group),
                           status_counts=dict(Counter(r['status'] for r in group)),
                           positive_candidates=len(positives),
                           exact_filled_signatures_among_positive=len({r['filled_signature'] for r in positives}),
                           exact_attempt_signatures_among_positive=len({r['attempt_signature'] for r in positives})))
    joint = []
    for size in sorted({r['requested_shares'] for r in rows}):
        for extra in EXTRA_COSTS:
            for fraction in FRACTIONS:
                g = [r for r in rows if r['requested_shares'] == size and r['extra_cost_per_retained_share'] == extra and r['missed_fraction_budget'] == fraction]
                by_id = defaultdict(list)
                for r in g:
                    by_id[r['candidate_id']].append(r)
                passing = [k for k, v in by_id.items() if len(v) == 3 and {r['latency_ms'] for r in v} == {500, 600, 1000} and all(r['positive'] for r in v)]
                joint.append({'shares': size, 'extra_cost': extra, 'missed_fraction': fraction,
                              'positive_all_three_delays': len(passing), 'candidate_ids': sorted(passing)})
    return {'by_scenario': result, 'joint_three_delay': joint}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--inputs', nargs='+', required=True)
    p.add_argument('--out', required=True)
    args = p.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    paths = [Path(x).resolve() for x in args.inputs]
    registration = {'analysis': 'post_development_adversarial_missing_fill_v1',
                    'created_at_utc': datetime.now(timezone.utc).isoformat(),
                    'inputs': [{'path': str(x), 'sha256': digest(x)} for x in paths],
                    'code_sha256': digest(__file__), 'fractions': FRACTIONS,
                    'extra_costs_per_share': EXTRA_COSTS,
                    'is_new_backtest': False, 'is_new_holdout': False,
                    'rules': 'Delete up to floor(fraction*original filled orders), ranked by positive net contribution after extra cost; keep losing fills; no replacement orders.',
                    'limitations': ['Outcomes are used adversarially, not predictively.',
                                    'No empirical execution probability or queue estimate.',
                                    'No cash-redeployment or shared-portfolio replay.',
                                    'Distinct signatures do not prove distinct mechanisms.',
                                    'Development outcomes were already viewed; this is not blind preregistration.']}
    write_json(out/'registration.json', registration)
    rows = []
    for x in paths:
        rows.extend(analyse_document(json.loads(x.read_text())))
    if [digest(x) for x in paths] != [x['sha256'] for x in registration['inputs']] or digest(__file__) != registration['code_sha256']:
        raise RuntimeError('inputs or code changed during analysis')
    write_json(out/'results.json', rows)
    write_json(out/'summary.json', aggregate(rows))
    columns = sorted(set().union(*(r.keys() for r in rows)) - {'removed_market_ids', 'original_status_counts'})
    with (out/'results.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    print(json.dumps({'output': str(out), 'rows': len(rows), 'new_backtests': 0, 'new_OOS': 0}))


if __name__ == '__main__':
    main()
