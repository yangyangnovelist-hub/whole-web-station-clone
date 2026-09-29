"""Replay recorded days (outcometick layout, one or many days) through the engine.

    python -m bot replay <data root> --log replay.jsonl

The layout is the one the free sample ships and `python -m bot record`
writes, so recorded days and the sample replay the same way.
"""
from __future__ import annotations

import csv
import gzip
import heapq
import itertools
import json
import zlib
from pathlib import Path

from .events import FIXED_POINT, Market, Spot, Tick, Twap, market_from_gamma, parse_clob

CLOB_KINDS = ("book", "best_bid_ask", "price_change", "last_trade_price")


def _lines(path):
    """Lines of a (possibly multi-member, possibly truncated) gzip file."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield line
    except (EOFError, gzip.BadGzipFile, zlib.error):
        return  # a recorder killed mid-write leaves a truncated last member


def files(root, kind):
    root = Path(root)
    if kind in CLOB_KINDS or kind == "markets":
        pat = f"polymarket/daily/{kind}/*/*-{kind}-*.jsonl.gz"
    elif kind == "spot":
        pat = "chainlink/daily/prices/*/*-prices-*.csv.gz"
    elif kind == "twap":
        pat = "chainlink-twap-60s/daily/prices/*/*-twap60s-prices-*.csv.gz"
    else:
        raise ValueError(kind)
    hits = list(root.glob(pat)) or list(root.glob("data/" + pat))   # the sample nests under data/
    return sorted(hits, key=lambda p: p.name.rsplit("-", 3)[-3:])


def load_markets(root, days=None):
    out = {}
    for path in files(root, "markets"):
        for line in _lines(path):
            d = json.loads(line)
            strike = int(d["strike_value"]) / FIXED_POINT if d.get("strike_value") else None
            m = market_from_gamma(d["raw"], strike=strike)
            prices = [float(p) for p in d.get("outcome_prices") or ()]
            if d.get("resolved") and sorted(prices) == [0.0, 1.0]:
                up_i = d["token_ids"].index(m.up_token)
                m.up_won = prices[up_i] == 1.0
            if days and _day(m.start) not in days:
                continue
            out[m.slug] = m
    return out


def _day(sec):
    import time
    return time.strftime("%Y-%m-%d", time.gmtime(sec))


def _clob_stream(path):
    for line in _lines(path):
        d = json.loads(line)
        recv = int(d["recv_ms"])
        for ev in parse_clob(d["payload"], recv):
            yield ev


def _csv_stream(path, cls):
    rows = []
    for line in _lines_csv(path):
        rows.append(line)
    rows.sort(key=lambda r: int(r["recv_ms"]))       # a few ticks arrive <1s out of order
    for r in rows:
        sec = int(r["feed_ts_ms"]) // 1000
        if cls is Spot:
            yield Spot(int(r["recv_ms"]), sec, float(r["value"]))
        else:
            full = r.get("full_accuracy_value")
            yield Twap(int(r["recv_ms"]), sec, float(r["value"]), int(full) if full else None)


def _lines_csv(path):
    return csv.DictReader(_lines(path))


def events(root, kinds=CLOB_KINDS + ("spot", "twap"), tick_ms=500):
    """Every event of every day under `root`, merged by receive time, with clock ticks."""
    streams = []
    for kind in kinds:
        for path in files(root, kind):
            if kind == "spot":
                streams.append(_csv_stream(path, Spot))
            elif kind == "twap":
                streams.append(_csv_stream(path, Twap))
            else:
                streams.append(_clob_stream(path))
    counter = itertools.count()
    merged = heapq.merge(*[((ev.recv_ms, next(counter), ev) for ev in s) for s in streams])
    next_tick = None
    for recv, _, ev in merged:
        if next_tick is None:
            next_tick = recv - recv % tick_ms + tick_ms
        while recv >= next_tick:
            yield Tick(next_tick)
            next_tick += tick_ms
        yield ev


def run(root, engine, kinds=None, days=None, progress=None):
    markets = load_markets(root, days)
    for m in markets.values():
        engine.add_market(m)
    n = 0
    for ev in events(root, kinds or CLOB_KINDS + ("spot", "twap")):
        engine.on_event(ev)
        n += 1
        if progress and n % 1_000_000 == 0:
            progress(n, ev.recv_ms)
    engine.settle_ready(float("inf"))
    return markets, n
