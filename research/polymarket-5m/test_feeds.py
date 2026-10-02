import gzip
import json
from datetime import datetime, timezone

import numpy as np
import pytest

import feeds


def test_parse_each_source():
    bbo = {}
    r = 1_000_000_000_000
    m = feeds.parse("binance_spot", json.dumps({"stream": "btcusdt@trade", "data": {
        "e": "trade", "E": 1, "T": 1700000000123, "p": "60000.5", "q": "0.01"}}), r, bbo)
    assert m == [{"s": "binance_spot", "r": r, "e": 1700000000123, "p": 60000.5, "q": 0.01}]
    tick = json.dumps({"stream": "btcusdt@bookTicker", "data": {"u": 1, "s": "BTCUSDT", "b": "1", "B": "2", "a": "3", "A": "4"}})
    assert len(feeds.parse("binance_spot", tick, r, bbo)) == 1 and feeds.parse("binance_spot", tick, r, bbo) == []
    m = feeds.parse("binance_perp", json.dumps({"data": {"e": "aggTrade", "T": 5, "p": "2", "q": "3"}}), r, bbo)
    assert m[0]["e"] == 5 and m[0]["p"] == 2.0
    m = feeds.parse("okx", json.dumps({"arg": {}, "data": [{"ts": "7", "px": "1.5", "sz": "2"}]}), r, bbo)
    assert m == [{"s": "okx", "r": r, "e": 7, "p": 1.5, "q": 2.0}]
    m = feeds.parse("bybit", json.dumps({"topic": "publicTrade.BTCUSDT", "data": [{"T": 8, "p": "3", "v": "4"}]}), r, bbo)
    assert m[0]["e"] == 8 and m[0]["q"] == 4.0
    assert feeds.parse("bybit", '{"op":"pong"}', r, bbo) == []
    m = feeds.parse("deribit", json.dumps({"method": "subscription", "params": {"data": [
        {"timestamp": 9, "price": 5.0, "amount": 10}]}}), r, bbo)
    assert m[0]["e"] == 9 and m[0]["p"] == 5.0
    m = feeds.parse("coinbase", json.dumps({"type": "match", "time": "2026-10-02T00:00:00.123Z", "price": "6", "size": "1"}), r, bbo)
    assert m[0]["e"] == pytest.approx(datetime(2026, 10, 2, tzinfo=timezone.utc).timestamp() * 1000 + 123)
    assert feeds.parse("coinbase", '{"type":"subscriptions"}', r, bbo) == []


def test_analyze_first_arrival(tmp_path):
    rng = np.random.default_rng(0)
    n = 30_000  # one print every 100 ms for 50 minutes
    e = 1_790_000_000_000 + 100 * np.arange(n)
    lp = np.cumsum(rng.normal(0, 2e-5, n))
    lp[np.arange(8000, n, 1000)] += 0.004  # jumps far above 2 sigma
    lp = np.cumsum(np.r_[lp[0], np.diff(lp)])
    f = tmp_path / "f.jsonl.gz"
    with gzip.open(f, "wt") as g:
        for src, delay in (("binance_spot", 120), ("okx", 80)):
            for ei, x in zip(e, lp):
                g.write(json.dumps({"s": src, "r": int((ei + delay) * 1e6), "e": int(ei), "p": float(60000 * np.exp(x)), "q": 1.0}) + "\n")
        g.write(json.dumps({"s": "polymarket_rtt", "r": 0, "ms": 12.0, "status": 200}) + "\n")
    text = feeds.analyze(f)
    assert "| okx | 30,000 | 80 | 80 | 80 |" in text and "| binance_spot | 30,000 | 120 |" in text
    assert "往返（长连接）：中位 12.0 ms" in text
    row = next(l for l in text.splitlines() if l.startswith("| okx |") and "%" in l)
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert cells[2] == "100%" and cells[3] == "0" and cells[4] == "40"
    row = next(l for l in text.splitlines() if l.startswith("| binance_spot |") and "%" in l)
    assert [c.strip() for c in row.strip("|").split("|")][3] == "40"
