"""Convert strict BTC 5m receipt tapes to the mining research interchange.

This adapter never resamples the execution book, never substitutes an external
OHLC direction for settlement, and always labels its output development data.
The one-second grid applies only to signal decisions. Missing inputs remain
None. It is deliberately separate from every previously frozen evaluator.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import heapq
import itertools
import json
import math
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path

import h_replay_archive as archive

WINDOWS = (5, 15, 30, 60)


def finite(value):
    return value is not None and math.isfinite(float(value))


@dataclass
class Book:
    bids: dict = field(default_factory=dict)
    asks: dict = field(default_factory=dict)
    recv_ms: float | None = None
    source_ms: float = -math.inf
    ready: bool = False

    def apply(self, event):
        source_ms = float(event['source_ts_ms'])
        if source_ms < self.source_ms:
            return False  # receipt order retained; late venue state cannot rewind a token
        self.source_ms = source_ms
        self.recv_ms = float(event['recv_ms'])
        if event['kind'] == 'clob_snapshot':
            self.bids = self.levels(event['bids'])
            self.asks = self.levels(event['asks'])
            self.ready = True
        elif self.ready:
            price, size = float(event['price']), float(event['size'])
            levels = self.bids if event['side'] == 'BUY' else self.asks
            if size:
                levels[price] = size
            else:
                levels.pop(price, None)
        self.ready = bool(self.ready and self.bids and self.asks
                          and max(self.bids) < min(self.asks))
        return True

    @staticmethod
    def levels(raw):
        result = {}
        for item in raw:
            price, size = float(item['price']), float(item['size'])
            if not (finite(price) and finite(size) and 0 <= price <= 1 and size >= 0):
                raise ValueError('invalid book level')
            if size:
                result[price] = size
        return result

    def fresh(self, now, age):
        return (self.ready and self.recv_ms is not None
                and 0 <= now - self.recv_ms <= age)


class Adapter:
    def __init__(self, markets, *, book_fresh_ms=250, source_fresh_ms=1000):
        self.markets = {str(m['market_id']): m for m in markets}
        self.token_market = {}
        for market_id, market in self.markets.items():
            for side in ('up', 'down'):
                token = str(market[f'{side}_token_id'])
                if token in self.token_market:
                    raise ValueError('token belongs to multiple markets')
                self.token_market[token] = market_id
        self.books = {}
        self.book_fresh_ms = float(book_fresh_ms)
        self.source_fresh_ms = float(source_fresh_ms)
        self.source_connected_at = None
        self.source_price = None
        self.source_recv = None
        self.prices = deque(maxlen=62)
        self.pm_prices = {mid: deque(maxlen=62) for mid in self.markets}
        self.trades = deque()

    def book_event(self, market_id, now):
        market = self.markets[market_id]
        up = self.books.get(market['up_token_id'], Book())
        down = self.books.get(market['down_token_id'], Book())
        fresh_up = up.fresh(now, self.book_fresh_ms)
        fresh_down = down.fresh(now, self.book_fresh_ms)
        return {
            'type': 'book', 'market_id': market_id, 'recv_ms': now,
            'up_asks': sorted(up.asks.items()) if up.ready else [],
            'down_asks': sorted(down.asks.items()) if down.ready else [],
            'up_bids': sorted(up.bids.items(), reverse=True) if up.ready else [],
            'down_bids': sorted(down.bids.items(), reverse=True) if down.ready else [],
            'up_recv_ms': up.recv_ms, 'down_recv_ms': down.recv_ms,
            'healthy': fresh_up and fresh_down,
            'up_healthy': fresh_up, 'down_healthy': fresh_down,
            'max_book_age_ms': self.book_fresh_ms,
            'fee_rate': market.get('fee_rate'),
            'fee_provenance': market.get('fee_provenance', 'missing'),
        }

    def consume(self, event, *, emit_books=True):
        kind, now = event['kind'], float(event['recv_ms'])
        if kind in ('spot_connection', 'spot_disconnect'):
            self.source_connected_at = now if kind == 'spot_connection' else None
            self.source_price = self.source_recv = None
            self.prices.clear()
            self.trades.clear()
        elif kind == 'spot_trade' and self.source_connected_at is not None:
            price = float(event['price'])
            if not finite(price) or price <= 0:
                raise ValueError('invalid external price')
            self.source_price, self.source_recv = price, now
            if kind == 'spot_trade':
                size = float(event['size']) * price  # catalog specifies quote volume
                # Only documented aggressor fields qualify. No side inference from price.
                side = event.get('aggressor_side')
                if side is None and isinstance(event.get('buyer_is_maker'), bool):
                    side = 'SELL' if event['buyer_is_maker'] else 'BUY'
                signed = size if side == 'BUY' else -size if side == 'SELL' else None
                self.trades.append((now, signed, size))
        elif kind in ('clob_connection', 'clob_error'):
            affected = {self.token_market[t] for t in self.books if t in self.token_market}
            self.books.clear()
            for history in self.pm_prices.values():
                history.clear()
            if emit_books:
                for market_id in sorted(affected):
                    yield self.book_event(market_id, now)
        elif kind in ('clob_snapshot', 'clob_price_change'):
            token = str(event['asset_id'])
            market_id = self.token_market.get(token)
            if market_id is not None:
                if str(event['market_id']) != market_id:
                    raise ValueError('token / market mapping mismatch')
                book = self.books.setdefault(token, Book())
                if book.apply(event) and emit_books:
                    yield self.book_event(market_id, now)

    @staticmethod
    def window(history, now, seconds):
        rows = list(history)
        wanted = seconds + 1
        if len(rows) < wanted:
            return None
        rows = rows[-wanted:]
        if any(t != now - (seconds-i)*1000 or value is None
               for i, (t, value, _available) in enumerate(rows)):
            return None
        return rows

    def decisions(self, now):
        current = self.source_price if (self.source_recv is not None
                    and 0 <= now-self.source_recv <= self.source_fresh_ms) else None
        self.prices.append((now, current, self.source_recv))
        while self.trades and self.trades[0][0] <= now-60_000:
            self.trades.popleft()
        external, ext_available = {}, {}
        for seconds in WINDOWS:
            rows = self.window(self.prices, now, seconds)
            key = f'r_{seconds}s'
            external[key] = 10000*math.log(rows[-1][1]/rows[0][1]) if rows else None
            ext_available[key] = max(r[2] for r in rows) if rows else None
            if seconds in (15, 60):
                key = f'rv_{seconds}s'
                external[key] = (10000*math.sqrt(sum(math.log(b[1]/a[1])**2
                    for a, b in zip(rows, rows[1:]))) if rows else None)
                ext_available[key] = max(r[2] for r in rows) if rows else None
                trades = [row for row in self.trades if now-seconds*1000 < row[0] <= now]
                valid = (current is not None and self.source_connected_at is not None
                         and self.source_connected_at <= now-seconds*1000 and trades
                         and all(row[1] is not None for row in trades))
                volume = sum(row[2] for row in trades)
                key = f'flow_{seconds}s'
                external[key] = sum(row[1] for row in trades)/volume if valid and volume > 0 else None
                ext_available[key] = max(row[0] for row in trades) if external[key] is not None else None
        for market_id, market in self.markets.items():
            if not (market['recv_ms'] <= now and market['start_ms'] <= now < market['end_ms']):
                continue
            up = self.books.get(market['up_token_id'], Book())
            fresh = up.fresh(now, self.book_fresh_ms)
            mid = (max(up.bids)+min(up.asks))/2 if fresh else None
            self.pm_prices[market_id].append((now, mid, up.recv_ms if fresh else None))
            features, available = dict(external), dict(ext_available)
            features.update(mid_up=mid, tau_s=(market['end_ms']-now)/1000,
                            imbalance_up=None, fair_up=None, tail_gap_bps=None,
                            tail_sigma_bps=None, twap_known_fraction=None)
            available.update(mid_up=up.recv_ms if fresh else None, tau_s=now,
                             imbalance_up=None, fair_up=None, tail_gap_bps=None,
                             tail_sigma_bps=None, twap_known_fraction=None)
            if fresh:
                bid_qty, ask_qty = up.bids[max(up.bids)], up.asks[min(up.asks)]
                features['imbalance_up'] = (bid_qty-ask_qty)/(bid_qty+ask_qty)
                available['imbalance_up'] = up.recv_ms
            for seconds in WINDOWS:
                key = f'pm_r_{seconds}s'
                rows = self.window(self.pm_prices[market_id], now, seconds)
                features[key] = rows[-1][1]-rows[0][1] if rows else None
                available[key] = max(row[2] for row in rows) if rows else None
            yield {'type':'decision', 'market_id':market_id, 'recv_ms':now,
                   'features':features, 'feature_available_ms':available}


def adapt_events(events, markets, *, start_ms, end_ms, book_fresh_ms=250, source_fresh_ms=1000,
                 latency_projection=False):
    """Events must already be in receipt order; same-clock frames precede grid decisions."""
    adapter = Adapter(markets, book_fresh_ms=book_fresh_ms, source_fresh_ms=source_fresh_ms)
    grid = math.ceil(start_ms/1000)*1000
    projection_times = (heapq.merge(range(int(grid), math.floor(end_ms)+1, 1000),
                                   range(int(grid)+500, math.floor(end_ms)+1, 1000),
                                   range(int(grid)+600, math.floor(end_ms)+1, 1000))
                        if latency_projection else None)
    next_projection = next(projection_times, None) if latency_projection else None

    def projected(at):
        for market_id, market in adapter.markets.items():
            if market['recv_ms'] <= at and market['start_ms'] <= at < market['end_ms']:
                row = adapter.book_event(market_id, at)
                row['projection_asof_ms'] = at
                yield row
        if at % 1000 == 0:
            yield from adapter.decisions(at)

    previous = -math.inf
    for now, group in itertools.groupby(events, key=lambda event: float(event['recv_ms'])):
        if not finite(now) or now < previous:
            raise ValueError('receipt clock moved backwards')
        if now > end_ms:
            raise ValueError('event past declared coverage end')
        if latency_projection:
            while next_projection is not None and next_projection < now:
                yield from projected(next_projection)
                next_projection = next(projection_times, None)
        else:
            while grid < now:
                yield from adapter.decisions(grid)
                grid += 1000
        for event in group:
            if latency_projection:
                for _ in adapter.consume(event, emit_books=False):
                    pass
            else:
                yield from adapter.consume(event)
        if latency_projection:
            if next_projection == now:
                yield from projected(next_projection)
                next_projection = next(projection_times, None)
        elif grid == now:
                yield from adapter.decisions(grid)
                grid += 1000
        previous = now
    if latency_projection:
        while next_projection is not None:
            yield from projected(next_projection)
            next_projection = next(projection_times, None)
    else:
        while grid <= end_ms:
            yield from adapter.decisions(grid)
            grid += 1000


def _sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def first_raw_metadata(path):
    """Restore mapping availability from raw Gamma receipts, never the final label."""
    earliest, signatures = {}, {}
    with gzip.open(path, 'rt', encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            slug = str(row.get('slug', ''))
            if not slug.startswith('btc-updown-5m-'):
                continue
            now = row.get('_recorded_at_ms')
            if not finite(now):
                raise ValueError('raw market missing local receipt clock')
            outcomes = row['outcomes']
            tokens = row['clobTokenIds']
            outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
            tokens = json.loads(tokens) if isinstance(tokens, str) else tokens
            if set(outcomes) != {'Up','Down'} or len(tokens) != 2:
                raise ValueError('raw market token mapping invalid')
            fee = row.get('feeSchedule')
            rate = None
            if fee is not None:
                if fee.get('exponent') != 1 or fee.get('takerOnly') is not True:
                    raise ValueError('unsupported raw market fee schedule')
                rate = float(fee['rate'])
            market_id = str(row['conditionId'])
            info = {'recv_ms':float(now),'slug':slug,
                    'up_token_id':str(tokens[outcomes.index('Up')]),
                    'down_token_id':str(tokens[outcomes.index('Down')]),
                    'fee_rate':rate, 'fee_provenance':'raw_gamma_feeSchedule' if rate is not None else 'missing',
                    'resolution_source_url':row.get('resolutionSource'),
                    'crypto_market_config':row.get('cryptoMarketConfig')}
            signature = (slug, info['up_token_id'], info['down_token_id'], rate,
                         info['resolution_source_url'], json.dumps(info['crypto_market_config'],sort_keys=True))
            if market_id in signatures and signatures[market_id] != signature:
                raise ValueError('market definition or fee changed; time-varying metadata adapter required')
            signatures[market_id] = signature
            if market_id not in earliest or now < earliest[market_id]['recv_ms']:
                earliest[market_id] = info
    return earliest


def convert(artifact, output, *, provenance, assumed_fee_rate=None, raw_markets=None,
            latency_projection=False):
    root, output = Path(artifact), Path(output)
    if (root/'strict'/'manifest.json').is_file():
        root = root/'strict'
    manifest = archive.validate_standard_artifact(root)['manifest']
    start_ms, end_ms = float(manifest['started_ms']), float(manifest['ended_ms'])
    if not (finite(start_ms) and finite(end_ms) and start_ms <= end_ms):
        raise ValueError('invalid coverage')
    if assumed_fee_rate is not None and not 0 <= assumed_fee_rate <= 1:
        raise ValueError('invalid assumed fee rate')
    raw_metadata = first_raw_metadata(raw_markets) if raw_markets else {}
    markets = []
    with gzip.open(root/'market_registry.csv.gz', 'rt', newline='') as stream:
        for row in csv.DictReader(stream):
            slug = row.get('slug', '')
            if not slug and str(manifest.get('coin','')).lower() == 'btc':
                # The original recording strict schema omits slug after verified BTC5m parsing.
                slug = 'btc-updown-5m-' + str(int(row['start_ts']))
            if not slug.startswith('btc-updown-5m-'):
                raise ValueError('strict registry contains a non-BTC-5m market')
            start = int(slug.rsplit('-',1)[1])*1000
            fee = row.get('fee_rate')
            if fee in (None, ''):
                fee, fee_source = assumed_fee_rate, 'assumed' if assumed_fee_rate is not None else 'missing'
            else:
                fee, fee_source = float(fee), 'artifact_market_registry'
            if fee is not None and not 0 <= fee <= 1:
                raise ValueError('invalid market fee rate')
            market = {'type':'market', 'market_id':str(row['market_id']),
                'recv_ms':float(row['updated_at'])*1000, 'start_ms':start,
                'end_ms':start+300_000, 'slug':slug,
                'up_token_id':str(row['up_token_id']), 'down_token_id':str(row['down_token_id']),
                'label_source':'gamma', 'fee_rate':fee, 'fee_provenance':fee_source}
            if raw_markets:
                raw = raw_metadata.get(market['market_id'])
                if raw is None or any(raw[k] != market[k] for k in ('slug','up_token_id','down_token_id')):
                    raise ValueError('raw Gamma metadata disagrees with strict registry')
                market.update(raw)
                if market['fee_rate'] is None and assumed_fee_rate is not None:
                    market.update(fee_rate=assumed_fee_rate, fee_provenance='assumed')
            markets.append(market)
    if not markets:
        raise ValueError('no BTC-5m markets')
    market_ids = {m['market_id'] for m in markets}
    outcomes = []
    for row in archive.iter_outcomes(root/'market_outcomes.csv.gz'):
        if row['resolution_source'] != 'gamma' or row['market_id'] not in market_ids:
            raise ValueError('non-Gamma or unmapped outcome')
        resolved = row['source_ts_ms']
        if not finite(resolved) or not finite(row['recv_ms']) or row['recv_ms'] < resolved:
            raise ValueError('invalid outcome knowledge time')
        outcomes.append({'type':'outcome','market_id':row['market_id'],
                         'recv_ms':row['recv_ms'], 'resolved_ms':resolved,
                         'winner':row['winner'], 'source':'gamma'})
    identity = _sha(root/'manifest.json')
    dataset = {'type':'dataset', 'recv_ms':min(start_ms, *(m['recv_ms'] for m in markets)),
               'dataset_id':manifest['run_id']+':'+identity[:16], 'split':'development',
               'provenance':{'source':provenance,'fee_assumption':assumed_fee_rate,
                             'raw_markets_sha256':_sha(Path(raw_markets)) if raw_markets else None},
               'coverage_end_ms':end_ms,
               'input_manifest_sha256':identity, 'input_schema':manifest['schema'],
               'decision_grid_ms':1000,
               'book_resolution':'decision_and_match_projection' if latency_projection else 'event_receipt',
               'supported_latencies_ms':[500,600,1000] if latency_projection else None,
               'external_price_selection':'latest_received_BTCUSDT_spot_trade_only',
               'coverage_start_ms':start_ms, 'observed_hours':(end_ms-start_ms)/3_600_000,
               'assumed_fee_rate':assumed_fee_rate,
               'limitations':['book depth gives an execution upper bound, not measured fills',
                  'fair_up and TWAP tail features unavailable without documented strike/TWAP samples',
                  'aggressor flow remains missing if strict converter omitted original trade side',
                  'empty/crossed token requires new snapshot; old strict rows lack atomic frame IDs, so capacity may be underestimated',
                  ('first raw Gamma receipt supplies mapping/fee availability' if raw_markets
                   else 'registry updated_at used conservatively as metadata availability')]}
    source = archive.iter_normalized_events(root/'source_events.jsonl.gz', family='source')
    clob = archive.iter_normalized_events(root/'clob_events.jsonl.gz', family='clob')
    events = heapq.merge(source, clob, key=lambda row:float(row['recv_ms']))
    converted = adapt_events(events, markets, start_ms=start_ms, end_ms=end_ms,
                             latency_projection=latency_projection)
    # Stable tie priority: metadata, books/decisions, then settlement knowledge.
    rows = heapq.merge(sorted(markets, key=lambda x:x['recv_ms']), converted,
                      sorted(outcomes,key=lambda x:x['recv_ms']), key=lambda x:x['recv_ms'])
    output.parent.mkdir(parents=True, exist_ok=True)
    counts = Counter()
    feature_present = Counter()
    stream = (gzip.open(output, 'wt', encoding='utf-8', compresslevel=1)
              if output.suffix == '.gz' else open(output, 'wt', encoding='utf-8'))
    with stream:
        stream.write(json.dumps(dataset, separators=(',',':'), allow_nan=False)+'\n')
        for row in rows:
            counts[row['type']] += 1
            if row['type'] == 'decision':
                feature_present.update(k for k,v in row['features'].items() if v is not None)
            stream.write(json.dumps(row,separators=(',',':'),allow_nan=False)+'\n')
    return {'output':str(output),'sha256':_sha(output),'dataset':dataset,
            'counts':dict(counts),'nonmissing_features':dict(feature_present),
            'status':'development_adapter_output_not_strategy_validation'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact',required=True)
    parser.add_argument('--out',required=True)
    parser.add_argument('--provenance',required=True,help='Published artifact URL or immutable identity')
    parser.add_argument('--assumed-fee-rate',type=float)
    parser.add_argument('--raw-markets',help='Raw Gamma JSONL.gz with _recorded_at_ms; no outcome backdating')
    parser.add_argument('--latency-projection',action='store_true',
                        help='Exact receipt-state projection for 1s decisions and 500/600/1000ms matching ONLY')
    args=parser.parse_args()
    print(json.dumps(convert(args.artifact,args.out,provenance=args.provenance,
                             assumed_fee_rate=args.assumed_fee_rate,raw_markets=args.raw_markets,
                             latency_projection=args.latency_projection),indent=2))


if __name__ == '__main__':
    main()
