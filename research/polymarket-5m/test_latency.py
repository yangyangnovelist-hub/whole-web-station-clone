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
    text = (tmp_path / "n.md").read_text()
    assert "按时间取前 20 笔，只作描述）：过期报价（z=2，0.4 秒后按卖一买）：20 笔" in text and "（已撤销，不判定）" in text


def _latency_dir(d, export, spot_lines, starts, book_step=1.0):
    """A recording directory like recording.LatencyFiles writes: the fixture's markets, a book
    that updates every `book_step` s (ask 0.50 until start+100.4, then 0.70) and Coinbase prints."""
    import shutil
    d.mkdir(parents=True)
    for f in export.iterdir():
        if "shadow_current" not in f.name and "poly_probability" not in f.name:
            shutil.copy(f, d / f.name)
    rows = []
    for i, s0 in enumerate(starts):
        for t in list(np.arange(s0, s0 + 300, book_step)) + [s0 + 100.4]:
            a = 0.50 if t < s0 + 100.4 else 0.70
            rows.append({"market_id": f"m{i}", "source_ts": t, "receive_ts": t + 0.01, "best_bid": a - 0.01,
                         "best_ask": a, "bids_json": f"[[{a - 0.01:.2f}, 40.0]]", "asks_json": f"[[{a:.2f}, 25.0]]"})
    pd.DataFrame(rows).sort_values("source_ts").to_csv(d / "runtime_1000.poly_probability_observations_v1.csv.gz",
                                                        index=False, compression="gzip")
    (d / "coinbase_trades.jsonl.gz").write_bytes(gzip.compress(("\n".join(spot_lines) + "\n").encode()))


def _coinbase_lines(export, lo=None, hi=None):
    raw = b"".join(q.read_bytes() for q in sorted(export.glob("shadow_current.jsonl.gz.part-*")))
    out = []
    for line in gzip.decompress(raw).decode().splitlines():
        e = json.loads(line)
        if (lo is None or e["trade_ts"] >= lo) and (hi is None or e["trade_ts"] < hi):
            out.append(line.replace('"BINANCE_AGG_TRADE"', '"COINBASE_TRADE"'))
    return out


def test_pooled_test_c(export, tmp_path, monkeypatch):
    """Two coins, each a copy of the fixture with its spot trades as Coinbase prints."""
    starts = [S0 + 300 * i for i in range(N)]
    for coin in ("btc", "eth"):
        _latency_dir(tmp_path / "rec" / "1" / "x" / f"bundle-{coin}" / "latency", export, _coinbase_lines(export), starts)
    monkeypatch.setattr(lt, "C_SINCE", "2026-09-01")
    monkeypatch.setattr(lt, "C_COINS", ("btc", "eth"))
    monkeypatch.setattr(lt, "C_N", 100)
    out = tmp_path / "real" / "c.md"
    lt.run_pooled([tmp_path / "rec"], out, reps=200)
    assert f"目前 {2 * N} 笔，不到 100 笔，不判定" in out.read_text()
    monkeypatch.setattr(lt, "C_N", 40)
    lt.run_pooled([tmp_path / "rec"], out, reps=200)
    text = out.read_text()
    assert "检验 C（前 40 笔）：过期报价（z=3，0.3 秒后按卖一买）：40 笔" in text and "| btc | 1 |" in text
    assert out.with_suffix(".verdict.md").exists() and len(pd.read_csv(out.with_suffix(".trades.csv"))) == 40
    monkeypatch.setattr(lt, "C_N", 45)  # a later run keeps the pinned verdict
    lt.run_pooled([tmp_path / "rec"], out, reps=200)
    assert "（已判定，不再重算）" in out.read_text() and "前 40 笔" in out.read_text()


def test_test_c_does_not_trigger_across_recording_gaps(export, tmp_path, monkeypatch):
    """Run 2 starts 20 min after run 1 ends and 30 s into a market: its first print must not be
    a trigger against run 1's last price, and a book that stopped updating must not fill."""
    starts = [S0 + 300 * i for i in range(N)]
    cut = S0 + 300 * 10
    lines = _coinbase_lines(export)
    first = [ln for ln in lines if json.loads(ln)["trade_ts"] < cut]
    second = [ln for ln in lines if json.loads(ln)["trade_ts"] >= cut + 1270]
    _latency_dir(tmp_path / "rec" / "1" / "x" / "bundle-btc" / "latency", export, first, starts[:10])
    _latency_dir(tmp_path / "rec" / "2" / "x" / "bundle-btc" / "latency", export, second, starts)
    monkeypatch.setattr(lt, "C_SINCE", "2026-09-01")
    dirs = {"btc": sorted((tmp_path / "rec").glob("*/x/bundle-btc/latency"))}
    t = lt.pooled_trades(dirs, "2026-09-01")
    run2_start = cut + 1270
    assert not ((t["t"] >= run2_start) & (t["t"] < run2_start + 1)).any()  # no trigger on run 2's first print
    assert set(t["run"]) == {"1", "2"} and t["market_id"].is_unique
    # the same data with a book that updates only every 60 s: nothing is traded
    shutil_dir = tmp_path / "sparse" / "1" / "x" / "bundle-btc" / "latency"
    _latency_dir(shutil_dir, export, lines, starts, book_step=60.0)
    assert lt.pooled_trades({"btc": [shutil_dir]}, "2026-09-01").empty


def test_test_d_trades_only_jumps_worth_the_threshold(export, tmp_path, monkeypatch):
    """Each market has one +0.4% jump at start+100 s (fair Up about 0.9) and 1e-5 noise elsewhere
    (a 2-sigma noise print moves the fair price by far less than 12c): one trade per market, at
    the 0.50 ask 0.3 s after the jump."""
    starts = [S0 + 300 * i for i in range(N)]
    _latency_dir(tmp_path / "rec" / "1" / "x" / "bundle-btc" / "latency", export, _coinbase_lines(export), starts)
    monkeypatch.setattr(lt, "D_SINCE", "2026-09-01")
    dirs = lt._latency_dirs([tmp_path / "rec"], ("btc",))
    t = lt._per_recording(dirs, "2026-09-01", "coinbase", lambda mk, sp, sg, bk: lt.gated_trades(mk, sp, sg, bk),
                          ["coin", "market_id"], [])
    assert len(t) == N and (t["price"] == 0.50).all() and (t["fair"] - t["price"] - t["fee"] >= 0.12).all()
    assert np.allclose(t["t"] - np.array(starts), 100.05)
    monkeypatch.setattr(lt, "D_N", 20)
    out = tmp_path / "real" / "d.md"
    lt.run_test_d([tmp_path / "rec"], out, reps=200)
    assert "检验 D（前 20 笔）：公平价筛选的过期报价（θ=12¢，z=2，0.3 秒后按卖一买）：20 笔" in out.read_text()


def test_candidates_across_a_clob_disconnect_are_dropped(export, tmp_path):
    """The recorder's socket closes at start+100.2 in the first market, after the quote shown at the
    jump was received and before the first update after the order: the frozen 0.50 ask is not
    bought there (a later print, seen once the book has moved to 0.70, may still trade). The other
    markets trade as before, and a close outside those two receipts changes nothing."""
    starts = [S0 + 300 * i for i in range(N)]
    x = tmp_path / "rec" / "1" / "x"
    _latency_dir(x / "bundle-btc" / "latency", export, _coinbase_lines(export), starts)
    (x / "raw" / "2026-09-30").mkdir(parents=True)
    rows = [{"at": int((starts[0] + 100.2) * 1000), "where": "clob", "err": "ConnectionClosedError(1013)"},
            {"at": int((starts[1] + 50.5) * 1000), "where": "clob", "err": "ConnectionClosedError(None)"},
            {"at": int((starts[2] + 100.2) * 1000), "where": "discover", "err": "timeout"}]
    (x / "raw" / "2026-09-30" / "errors.jsonl.gz").write_bytes(
        gzip.compress("".join(json.dumps(r) + "\n" for r in rows).encode()))
    dirs = lt._latency_dirs([tmp_path / "rec"], ("btc",))
    notes = []
    t = lt._per_recording(dirs, "2026-09-01", "coinbase", lambda mk, sp, sg, bk: lt.gated_trades(mk, sp, sg, bk),
                          ["coin", "market_id"], notes)
    first = t[t["market_id"] == "m0"]
    assert (first["price"] != 0.50).all()
    rest = t[t["market_id"] != "m0"]
    assert len(rest) == N - 1 and (rest["price"] == 0.50).all()
    assert any("dropped because the CLOB socket closed" in n and "(2 disconnects" in n for n in notes)


def test_book_across_close():
    books = pd.DataFrame({"market_id": "m", "ts": [10.0, 11.0, 12.0], "recv": [10.1, 11.1, 12.1],
                          "bid": 0.4, "ask": 0.5, "bid_size": 1.0, "ask_size": 1.0})
    assert not lt.Book(books).across_close("m", 11.2, 11.5)             # no log: nothing dropped
    assert lt.Book(books, [11.6]).across_close("m", 11.2, 11.5)         # closed between 11.1 and 12.1
    assert not lt.Book(books, [12.5]).across_close("m", 11.2, 11.5)     # closed after the next update
    assert not lt.Book(books, [11.0]).across_close("m", 11.2, 11.5)     # before the quote was received
    assert lt.Book(books, [13.0]).across_close("m", 12.2, 12.5)         # no update after: open-ended


def test_across_close_edges():
    books = pd.DataFrame({"market_id": ["m", "m", "m", "o"], "ts": [10.0, 11.0, 12.0, 11.6],
                          "recv": [10.1, 11.1, 12.1, 11.65], "bid": 0.4, "ask": 0.5, "bid_size": 1.0, "ask_size": 1.0})
    assert lt.Book(books, [11.1]).across_close("m", 11.2, 11.5)      # logged in the same ms as the quote's receipt
    # another market's update stamped after the order arrived at 11.65, before the close at 11.7:
    # the socket had delivered the order-time state, so the close does not matter for m
    assert not lt.Book(books, [11.7]).across_close("m", 11.2, 11.5)
    assert lt.Book(books, [11.62]).across_close("m", 11.2, 11.5)


def test_clob_close_times_count_clean_closes():
    import recording as rc
    rows = [{"at": 1000, "where": "clob-open"}, {"at": 5000, "where": "clob-open"},   # a clean close before 5 s
            {"at": 8000, "where": "clob", "err": "1013"}, {"at": 10000, "where": "clob-open"},
            {"at": 20000, "where": "clob-open"}, {"at": 21000, "where": "discover"}]
    assert rc.clob_close_times(rows) == [5.0, 8.0, 20.0]
    # a reconnect closed again within the same ms (as against a local server): log order decides,
    # so each close counts once and the next reconnect is not taken for a clean close
    same = [{"at": 1000, "where": "clob-open"}, {"at": 1001, "where": "clob"}, {"at": 3005, "where": "clob-open"},
            {"at": 3005, "where": "clob"}, {"at": 5010, "where": "clob-open"}, {"at": 5011, "where": "clob"}]
    assert rc.clob_close_times(same) == [1.001, 3.005, 5.011]


def test_load_closes_reads_a_truncated_log(tmp_path):
    d = tmp_path / "x" / "bundle-btc" / "latency"
    d.mkdir(parents=True)
    (tmp_path / "x" / "raw" / "2026-09-30").mkdir(parents=True)
    blob = gzip.compress("".join(json.dumps({"at": 1000 * i, "where": "clob"}) + "\n" for i in range(1, 51)).encode())
    (tmp_path / "x" / "raw" / "2026-09-30" / "errors.jsonl.gz").write_bytes(blob[:-12])  # no gzip trailer
    got = lt.load_closes(d)
    assert len(got) >= 40 and got[0] == 1.0


def test_diagnose_d_lists_test_d_trades(export, tmp_path, monkeypatch):
    starts = [S0 + 300 * i for i in range(N)]
    _latency_dir(tmp_path / "rec" / "1" / "x" / "bundle-btc" / "latency", export, _coinbase_lines(export), starts)
    monkeypatch.setattr(lt, "D_SINCE", "2026-09-01")
    out = tmp_path / "real" / "diag.md"
    lt.diagnose_d([tmp_path / "rec"], out)
    text = out.read_text()
    assert f"{N} 笔：" in text and "cb_z" in text


def test_test_f_triggers_on_binance_trades(export, tmp_path, monkeypatch):
    """The fixture's spot prints written as Binance trades trigger test F exactly as they trigger
    test D when written as Coinbase prints; test F ignores Coinbase prints."""
    starts = [S0 + 300 * i for i in range(N)]
    d = tmp_path / "rec" / "1" / "x" / "bundle-btc" / "latency"
    _latency_dir(d, export, _coinbase_lines(export), starts)
    lines = [l.replace('"COINBASE_TRADE"', '"BINANCE_WS_TRADE"') for l in _coinbase_lines(export)]
    (d / "binance_trades.jsonl.gz").write_bytes(gzip.compress(("\n".join(lines) + "\n").encode()))
    dirs = lt._latency_dirs([tmp_path / "rec"], lt.F_COINS)
    tf = lt._per_recording(dirs, "2026-09-01", lt.F_SPOT, lambda mk, sp, sg, bk: lt.gated_trades(mk, sp, sg, bk),
                           ["coin", "market_id"], [])
    td = lt._per_recording(dirs, "2026-09-01", lt.D_SPOT, lambda mk, sp, sg, bk: lt.gated_trades(mk, sp, sg, bk),
                           ["coin", "market_id"], [])
    assert len(tf) == N and tf[["t", "price", "pnl"]].equals(td[["t", "price", "pnl"]])
    (d / "coinbase_trades.jsonl.gz").unlink()
    monkeypatch.setattr(lt, "F_SINCE", "2026-09-01")
    monkeypatch.setattr(lt, "F_N", 5)
    out = tmp_path / "real" / "f.md"
    lt.run_test_f([tmp_path / "rec"], out, reps=200)
    assert "检验 F（前 5 笔）" in out.read_text()
