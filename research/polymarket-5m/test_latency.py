import gzip
import json

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

import latency as lt

S0 = 1_789_300_000 // 300 * 300
N = 30


def write_zst_parts(path_stem, df):
    buf = pa.BufferOutputStream()
    with pa.CompressedOutputStream(buf, "zstd") as f:
        f.write(df.to_csv(index=False).encode())
    (path_stem.parent / (path_stem.name + ".part-aa")).write_bytes(buf.getvalue().to_pybytes())


@pytest.fixture(scope="module")
def export(tmp_path_factory):
    """N markets: Binance jumps +0.4% at start+100; the Up ask sits at 0.50 until 0.4 s after the jump,
    then 0.70. Up wins in 80% of markets."""
    d = tmp_path_factory.mktemp("export")
    rng = np.random.default_rng(4)
    starts = [S0 + 300 * i for i in range(N)]
    reg = pd.DataFrame({"market_id": [f"m{i}" for i in range(N)], "start_ts": starts,
                        "up_token_id": "u", "down_token_id": "d", "updated_at": 0})
    out = pd.DataFrame({"market_id": reg["market_id"], "winner": ["Up" if i % 5 else "Down" for i in range(N)],
                        "resolution_ts": 0, "source": "x", "recorded_at": 0})
    rows = []
    for i, s in enumerate(starts):
        for t, a in ((s, 0.50), (s + 100.4, 0.70)):
            rows.append({"market_id": f"m{i}", "up_token_id": "u", "source_ts": t, "receive_ts": t + 0.01,
                         "sequence": 1, "best_bid": a - 0.01, "best_ask": a,
                         "bids_json": json.dumps([[round(a - 0.01, 2), 40.0]]), "asks_json": json.dumps([[a, 25.0]])})
    write_zst_parts(d / "runtime_1000.poly_probability_observations_v1.csv.zst", pd.DataFrame(rows))
    write_zst_parts(d / "runtime_1000.market_registry.csv.zst", reg)
    write_zst_parts(d / "runtime_1000.market_outcomes.csv.zst", out)
    secs = np.arange(S0 - 1200, S0 + 300 * N + 300)
    price = 80_000 * np.exp(np.cumsum(rng.normal(0, 1e-5, len(secs))))
    for s in starts:
        price[secs >= s + 100] *= 1.004
    lines = [json.dumps({"event": "BINANCE_AGG_TRADE", "trade_ts": float(t) + 0.05, "receive_ts": float(t) + 0.15,
                         "price": float(p)}) for t, p in zip(secs, price)]
    buf = gzip.compress(("\n".join(lines) + "\n").encode())
    (d / "shadow_current.jsonl.gz.part-aa").write_bytes(buf)
    return d


def test_triggers_and_reaction(export):
    books, markets, binance = lt.load(export)
    grid, sigma = lt.spot_grid(binance)
    trig = lt.triggers(markets, binance, sigma, 3.0)
    assert len(trig) == N and (trig["side"] == "Up").all()
    assert np.allclose(trig["t"], [S0 + 300 * i + 100.05 for i in range(N)])  # the trade's own time
    book = lt.Book(books)
    assert book.reaction("m0", S0 + 100, "Up") == pytest.approx(0.4)


def test_lag_decides_the_price(export):
    books, markets, binance = lt.load(export)
    grid, sigma = lt.spot_grid(binance)
    trig = lt.triggers(markets, binance, sigma, 3.0)
    book = lt.Book(books)
    fast, slow = lt.trade(trig, book, 0.2, False), lt.trade(trig, book, 1.0, False)
    assert (fast["price"] == 0.50).all() and (slow["price"] == 0.70).all()
    assert fast["pnl"].mean() > slow["pnl"].mean() and fast["size"].median() == 25
    assert lt.trade(trig, book, 1.0, True).empty  # the ask moved: the stale limit does not fill


def test_report(export, tmp_path):
    lt.run(export, tmp_path / "r.md", reps=500)
    text = (tmp_path / "r.md").read_text()
    assert "中位 100 ms" in text and "| 0.2 秒 |" in text


def test_book_rules_buy_the_favourite_at_its_ask(export):
    books, markets, binance = lt.load(export)
    book = lt.Book(books)
    t = lt.book_rule(markets, book, 199, 0.60, 0.80)  # at 101 s the Up ask is 0.70 (updated at 100.4)
    assert len(t) == N and (t["price"] == 0.70).all() and t["won"].mean() == pytest.approx(0.8)
    assert lt.book_rule(markets, book, 90, 0.60, 0.80).empty  # 110 s without an update: stale, no trade
    assert lt.book_rule(markets, book, 295, 0.40, 0.55).shape[0] == N  # at 5 s the 0.50 ask is fresh


def test_several_exports_count_each_row_once(export):
    one, two = lt.load(export), lt.load(export, export)
    assert all(len(a) == len(b) for a, b in zip(one, two))


def test_next_test_is_judged_once_on_the_first_n_trades(export, tmp_path, monkeypatch):
    monkeypatch.setattr(lt, "NEXT_SINCE", "2026-09-01")
    lt.run(export, tmp_path / "all.md", reps=200)
    assert "不适用" in (tmp_path / "all.md").read_text()
    monkeypatch.setattr(lt, "NEXT_N", 100)
    lt.run(export, tmp_path / "few.md", reps=200, since="2026-09-01")
    assert f"目前 {N} 笔，不到 100 笔，不判定" in (tmp_path / "few.md").read_text()
    monkeypatch.setattr(lt, "NEXT_N", 20)
    lt.run(export, tmp_path / "n.md", reps=200, since="2026-09-01")
    assert "按时间取前 20 笔）：过期报价（z=2，0.4 秒后按卖一买）：20 笔" in (tmp_path / "n.md").read_text()
