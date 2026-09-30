import io
import tarfile

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import cross

E = 1_787_145_300_000  # ms; a 5m and a 15m market end here


def parquet_bytes(df):
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), buf)
    return buf.getvalue()


@pytest.fixture
def archive(tmp_path):
    """5m (reference 100) and 15m (reference 101) ending at E. For one second the 5m Up ask is
    0.30 and the 15m Down ask 0.60 (box 0.90 + fees < 1); otherwise the box costs 1.02."""
    ts = np.arange(E - 300_000, E, 100)
    cheap = (ts >= E - 60_000) & (ts < E - 59_000)
    f5 = pd.DataFrame({"timestamp_ms": ts, "market_id": "m5", "lifecycle_state": "active",
                       "up_best_bid": 0.40, "up_best_ask": np.where(cheap, 0.30, 0.42),
                       "down_best_bid": 0.58, "down_best_ask": 0.60,
                       "up_ask_sizes": [[25.0, 5.0]] * len(ts), "down_ask_sizes": [[30.0]] * len(ts)})
    f15 = pd.DataFrame({"timestamp_ms": ts, "market_id": "m15", "lifecycle_state": "active",
                        "up_best_bid": 0.38, "up_best_ask": 0.40, "down_best_bid": 0.58, "down_best_ask": 0.60,
                        "up_ask_sizes": [[10.0]] * len(ts), "down_ask_sizes": [[40.0]] * len(ts)})
    mk = pd.DataFrame({"timestamp_ms": [E, E], "market_id": ["m5", "m15"], "slug": ["btc-updown-5m-x", "btc-updown-15m-x"],
                       "session_start_ts": [E - 300_000, E - 900_000], "session_end_ts": [E, E],
                       "chainlink_open_price": [100.0, 101.0], "up_won": [1.0, 0.0], "lifecycle_state": ["closed"] * 2})
    path = tmp_path / "market_parquet_2026-08-19.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for name, df in (("dataset=polymarket_features_100ms/date=2026-08-19/part-1.parquet", f5),
                         ("dataset=polymarket_features_100ms/date=2026-08-19/part-2.parquet", f15),
                         ("dataset=polymarket_market_100ms/date=2026-08-19/part-3.parquet", mk)):
            raw = parquet_bytes(df)
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    return path


def test_box_on_the_lower_reference(archive):
    feat, mk, rs = cross.read_day(archive)
    assert set(feat["market_id"]) == {"m5", "m15"} and feat["up_ask_size"].iloc[0] == 25.0
    mkts = cross.market_table(mk, rs)
    assert dict(zip(mkts["market_id"], mkts["horizon"])) == {"m5": 5, "m15": 15}
    b = cross.boxes(feat, mkts)
    assert len(b) == 1
    r = b.iloc[0]
    fee = 0.07 * 0.3 * 0.7 + 0.07 * 0.6 * 0.4
    assert r["pair"] == "5m-15m" and r["min_cost"] == pytest.approx(0.90 + fee)
    assert r["n_persist"] == 9 and r["persist_size"] == 25.0 and r["tau_best"] == pytest.approx(60.0)
    assert r["payoff"] == 2.0  # Up won on the 5m (final >= 100) and Down on the 15m (final < 101)


def test_stale_quote_timing():
    """Binance jumps +0.4% at E-100 s (received 150 ms later); the 5m Up ask stays 0.50 until
    400 ms after receipt, then 0.70. Up wins."""
    start = E - 300_000
    ts = np.arange(start, E, 100)
    t_jump = E - 100_000
    feat = pd.DataFrame({"timestamp_ms": ts, "market_id": "m5", "lifecycle_state": "active",
                         "up_best_bid": 0.49, "up_best_ask": np.where(ts < t_jump + 150 + 400, 0.50, 0.70),
                         "down_best_bid": 0.49, "down_best_ask": 0.51, "up_ask_size": 25.0, "down_ask_size": 30.0})
    mkts = pd.DataFrame({"market_id": ["m5"], "start": [start], "end": [E], "k": [100.0], "up_won": [1.0], "horizon": [5]})
    rng = np.random.default_rng(3)
    tt = np.arange(E - 1_200_000, E, 250)
    price = 80_000 * np.exp(np.cumsum(rng.normal(0, 1e-7, len(tt))))
    price[tt >= t_jump] *= 1.004
    binance = pd.DataFrame({"trade_ts_ms": tt, "recv_ts_ms": tt + 150, "price": price})
    t = cross.stale_trades(feat, mkts, binance, zs=(6.0,), lags=(0, 200, 500))
    assert list(t["t0"].unique()) == [t_jump + 150] and set(t["horizon"]) == {5}
    assert dict(zip(t["lag"], t["price"])) == {0: 0.50, 200: 0.50, 500: 0.70}
    assert t["still"].tolist() == [True, True, False]


def test_maker_queue_fills():
    """Up bid 0.60 with 100 shares ahead: sells of 60 then 50 at 0.60 fill a joining order on the
    second print; an order one tick above (0.61) fills on the first print (a sell below it)."""
    start = E - 300_000
    ts = np.arange(start, E, 100)
    feat = pd.DataFrame({"timestamp_ms": ts, "market_id": "m5", "lifecycle_state": "active",
                         "up_best_bid": 0.60, "up_best_ask": 0.62, "down_best_bid": 0.38, "down_best_ask": 0.40,
                         "up_ask_size": 20.0, "down_ask_size": 20.0, "up_bid_size": 100.0, "down_bid_size": 50.0})
    mkts = pd.DataFrame({"market_id": ["m5"], "start": [start], "end": [E], "k": [100.0], "up_won": [1.0],
                         "horizon": [5], "up_token": ["u"], "down_token": ["d"]})
    trades = pd.DataFrame({"recv_ts_ms": [E - 80_000, E - 70_000], "instrument": ["u", "u"], "price": [0.60, 0.60],
                           "size": [60.0, 50.0], "taker_side": ["sell", "sell"]})
    o = cross.maker_orders(feat, mkts, trades, taus=(90,))
    fav = o[o["which"] == "fav"].set_index("mode")
    assert fav.loc["join", "filled"] and fav.loc["join", "fill_s"] == pytest.approx(19.8)
    assert fav.loc["improve", "price"] == 0.61 and fav.loc["improve", "fill_s"] == pytest.approx(9.8)
    assert fav.loc["join", "pnl"] == pytest.approx(0.40)
    dog = o[o["which"] == "dog"]
    assert len(dog) == 2 and not dog["filled"].any()  # nobody sold Down


def test_gated_trades_price_the_jump():
    """A +0.4% Binance jump at E-100 s makes the fair Up price about 1; the ask is 0.50 until
    400 ms after receipt, then 0.70. Every threshold qualifies at both lags, at the ask then."""
    start = E - 300_000
    ts = np.arange(start, E, 100)
    t_jump = E - 100_000
    feat = pd.DataFrame({"timestamp_ms": ts, "market_id": "m5", "lifecycle_state": "active",
                         "up_best_bid": 0.49, "up_best_ask": np.where(ts < t_jump + 150 + 400, 0.50, 0.70),
                         "down_best_bid": 0.49, "down_best_ask": 0.51, "up_ask_size": 25.0, "down_ask_size": 30.0})
    mkts = pd.DataFrame({"market_id": ["m5"], "start": [start], "end": [E], "k": [100.0], "up_won": [1.0], "horizon": [5]})
    rng = np.random.default_rng(3)
    tt = np.arange(E - 1_200_000, E, 250)
    price = 80_000 * np.exp(np.cumsum(rng.normal(0, 1e-7, len(tt))))
    price[tt >= t_jump] *= 1.004
    binance = pd.DataFrame({"trade_ts_ms": tt, "recv_ts_ms": tt + 150, "price": price})
    g = cross.gated_trades(feat, mkts, binance, z0=6.0)
    assert len(g) == len(cross.GATE_LAGS) * len(cross.GATE_THETAS) and (g["t0"] == t_jump + 150).all()
    assert (g["fair"] > 0.99).all() and dict(zip(g["lag"], g["price"])) == {300: 0.50, 500: 0.70}
    assert g.loc[g["lag"] == 300, "pnl"].iloc[0] == pytest.approx(1 - 0.50 - 0.07 * 0.25)


def test_gated_trades_on_15m_markets():
    """The same jump in a 15m market 600 s before its end (outside the 5m window): only the
    15m run trades it, and its fair price is lower than a 5m market's would be 100 s out."""
    start = E - 900_000
    ts = np.arange(start, E, 100)
    t_jump = E - 600_000
    feat = pd.DataFrame({"timestamp_ms": ts, "market_id": "m15", "lifecycle_state": "active",
                         "up_best_bid": 0.49, "up_best_ask": 0.50,
                         "down_best_bid": 0.49, "down_best_ask": 0.51, "up_ask_size": 25.0, "down_ask_size": 30.0})
    mkts = pd.DataFrame({"market_id": ["m15"], "start": [start], "end": [E], "k": [100.0], "up_won": [1.0], "horizon": [15]})
    rng = np.random.default_rng(3)
    tt = np.arange(E - 1_800_000, E, 250)
    price = 80_000 * np.exp(np.cumsum(rng.normal(0, 6.7e-6, len(tt))))
    price[tt >= t_jump] *= 1.0004  # about 30 sigma of one second, 1.3 of the TWAP still ahead
    binance = pd.DataFrame({"trade_ts_ms": tt, "recv_ts_ms": tt + 150, "price": price})
    assert cross.gated_trades(feat, mkts, binance, z0=6.0).empty  # horizon 5 by default
    g = cross.gated_trades(feat, mkts, binance, z0=6.0, horizon=15)
    assert len(g) and (g["t0"] == t_jump + 150).all() and g["tau"].tolist() == pytest.approx([599.85] * len(g))
    sg = binance.set_index(binance["trade_ts_ms"] // 1000)["price"].pipe(np.log).groupby(level=0).last().diff() \
        .rolling(600, min_periods=300).std().loc[t_jump // 1000 - 1]
    import binary as bo
    from scipy.stats import norm
    fair = norm.cdf(np.log(1.0004) / (sg * bo.twap_std_factor(300.15, window=900)))
    assert g["fair"].iloc[0] == pytest.approx(fair, abs=0.02) and 0.6 < fair < 0.99


def test_maker_fill_tagged_by_a_preceding_adverse_move():
    """Binance drops 0.3% half a second before the join order on Up fills at E-70 s: tagged adverse;
    the improve order filled at E-80 s saw no move before it: not adverse."""
    start = E - 300_000
    ts = np.arange(start, E, 100)
    feat = pd.DataFrame({"timestamp_ms": ts, "market_id": "m5", "lifecycle_state": "active",
                         "up_best_bid": 0.60, "up_best_ask": 0.62, "down_best_bid": 0.38, "down_best_ask": 0.40,
                         "up_ask_size": 20.0, "down_ask_size": 20.0, "up_bid_size": 100.0, "down_bid_size": 50.0})
    mkts = pd.DataFrame({"market_id": ["m5"], "start": [start], "end": [E], "k": [100.0], "up_won": [1.0],
                         "horizon": [5], "up_token": ["u"], "down_token": ["d"]})
    trades = pd.DataFrame({"recv_ts_ms": [E - 80_000, E - 70_000], "instrument": ["u", "u"], "price": [0.60, 0.60],
                           "size": [60.0, 50.0], "taker_side": ["sell", "sell"]})
    rng = np.random.default_rng(1)
    tt = np.arange(E - 1_200_000, E, 100)
    price = 80_000 * np.exp(np.cumsum(rng.normal(0, 2e-6, len(tt))))
    price[tt >= E - 70_500] *= 0.997
    binance = pd.DataFrame({"trade_ts_ms": tt, "recv_ts_ms": tt + 50, "price": price})
    o = cross.maker_orders(feat, mkts, trades, taus=(90,), binance=binance)
    fav = o[o["which"] == "fav"].set_index("mode")
    assert fav.loc["join", "adverse"] == 1.0 and fav.loc["improve", "adverse"] == 0.0


def test_open_trades_price_the_twap_reference():
    """Binance stays flat for 20 minutes, then is 0.3% higher from the open on: the fair Up price
    just after the open is above 0.9 against a reference averaged before the open; Up at 0.60 is bought."""
    start = E - 300_000
    ts = np.arange(start, E, 100)
    feat = pd.DataFrame({"timestamp_ms": ts, "market_id": "m5", "lifecycle_state": "active",
                         "up_best_bid": 0.58, "up_best_ask": 0.60, "down_best_bid": 0.40, "down_best_ask": 0.42,
                         "up_ask_size": 20.0, "down_ask_size": 20.0})
    mkts = pd.DataFrame({"market_id": ["m5"], "start": [start], "end": [E], "k": [100.0], "up_won": [1.0], "horizon": [5]})
    rng = np.random.default_rng(2)
    tt = np.arange(start - 1_200_000, E, 250)
    price = 80_000 * np.exp(np.cumsum(rng.normal(0, 2e-6, len(tt))))
    price[tt >= start - 1000] *= 1.003
    binance = pd.DataFrame({"trade_ts_ms": tt, "recv_ts_ms": tt + 100, "price": price})
    o = cross.open_trades(feat, mkts, binance, delays=(3,))
    up = o[o["side"] == "Up"]
    assert len(up) == len(cross.OPEN_THETAS) and (up["p_up"] > 0.9).all() and (up["price"] == 0.60).all()
    assert (o["side"] == "Up").all()


def test_http_file_reads_parquet_by_ranges(monkeypatch):
    """HttpFile serves pyarrow the footer and one row group through Range requests only."""
    import urllib.request
    df = pd.DataFrame({"ts_ms": np.arange(3000, dtype=np.int64), "x": np.arange(3000) * 0.5})
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), buf, row_group_size=1000)
    blob = buf.getvalue()
    asked = []

    class Resp(io.BytesIO):
        status = 200

        def __init__(self, data, headers=None):
            super().__init__(data)
            self.headers = headers or {}

        def geturl(self):
            return "https://cdn.example/file"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=None):
        if req.get_method() == "HEAD":
            return Resp(b"", {"Content-Length": str(len(blob))})
        a, b = map(int, req.headers["Range"].split("=")[1].split("-"))
        asked.append((a, b))
        return Resp(blob[a:b + 1])

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    pf, raw = cross.remote_parquet("https://hf.example/x.parquet")
    rg = cross._rg_range(pf, "ts_ms")
    assert [(n, lo, hi) for _, n, lo, hi in rg] == [(1000, 0, 999), (1000, 1000, 1999), (1000, 2000, 2999)]
    t = pf.read_row_group(2).to_pandas()
    assert t["ts_ms"].tolist() == list(range(2000, 3000)) and t["x"].iloc[-1] == 1499.5
    assert raw.fetched <= 2 * len(blob) and all(b < len(blob) for _, b in asked)
