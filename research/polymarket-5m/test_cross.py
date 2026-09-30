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


def test_hourly_trades_price_against_the_binance_candle():
    """A 1h market opens at K = 80,000 on Binance and trades around K (no drift) until 230 s before
    the end, when it jumps 0.5%: fair Up goes from about 0.5 to about 1. The Up ask stays at 0.60
    until 800 ms after the next second; the first grid second that has received the jump buys Up at
    0.60 at every threshold in the 300-60 s window, nothing trades earlier, and in the last minute
    only the 2c threshold still pays for the 0.97 ask. The Binance candle agrees with the outcome."""
    H = 3_600_000
    start, end = E - H, E
    ts = np.arange(start, end, 100)
    t_jump = end - 230_000
    feat = pd.DataFrame({"timestamp_ms": ts, "market_id": "h1", "lifecycle_state": "active",
                         "up_best_bid": 0.45, "up_best_ask": np.where(ts < t_jump + 30_800, 0.60, 0.97),
                         "down_best_bid": 0.40, "down_best_ask": 0.55, "up_ask_size": 25.0, "down_ask_size": 30.0})
    mkts = pd.DataFrame({"market_id": ["h1"], "start": [start], "end": [end], "k": [np.nan], "up_won": [1.0],
                         "horizon": [60]})
    rng = np.random.default_rng(5)
    tt = np.arange(start - 1_200_000, end + 5_000, 250)
    price = 80_000 * np.exp(rng.normal(0, 2e-6, len(tt)))
    price[np.searchsorted(tt, start)] = 80_000.0
    price[tt >= t_jump] *= 1.005
    binance = pd.DataFrame({"trade_ts_ms": tt, "recv_ts_ms": tt + 150, "price": price})
    t, ck = cross.hourly_trades(feat, mkts, binance, lags=(300,))
    assert ck[["binance_up", "up_won"]].values.tolist() == [[1.0, 1.0]]
    assert not (t["window"] == "1800-300").any()
    w = t[t["window"] == "300-60"]
    assert len(w) == len(cross.HOUR_THETAS) and (w["side"] == "Up").all() and (w["price"] == 0.60).all()
    assert (w["fair"] > 0.99).all() and (w["t"] == end - 229_000).all()
    assert w["pnl"].iloc[0] == pytest.approx(1 - 0.60 - 0.07 * 0.6 * 0.4)
    last = t[t["window"] == "60-5"]
    assert last["theta"].tolist() == [0.02] and last["price"].tolist() == [0.97]



def _gated_fixture(live):
    """The gated jump fixture; with `live` the Up ask size ticks every 100 ms, as a running feed
    does, otherwise the book repeats one state (a stalled recorder) apart from the ask change."""
    start = E - 300_000
    ts = np.arange(start, E, 100)
    t_jump = E - 100_000
    size = 25.0 + (np.arange(len(ts)) % 7 if live else 0)
    feat = pd.DataFrame({"timestamp_ms": ts, "market_id": "m5", "lifecycle_state": "active",
                         "up_best_bid": 0.49, "up_best_ask": np.where(ts < t_jump + 150 + 400, 0.50, 0.70),
                         "down_best_bid": 0.49, "down_best_ask": 0.51, "up_ask_size": size, "down_ask_size": 30.0,
                         "up_bid_size": 10.0, "down_bid_size": 10.0})
    mkts = pd.DataFrame({"market_id": ["m5"], "start": [start], "end": [E], "k": [100.0], "up_won": [1.0], "horizon": [5]})
    rng = np.random.default_rng(3)
    tt = np.arange(E - 1_200_000, E, 250)
    price = 80_000 * np.exp(np.cumsum(rng.normal(0, 1e-7, len(tt))))
    price[tt >= t_jump] *= 1.004
    return feat, mkts, pd.DataFrame({"trade_ts_ms": tt, "recv_ts_ms": tt + 150, "price": price})


def test_book_health_keeps_live_books_and_drops_stalled_or_crossed_ones():
    feat, mkts, binance = _gated_fixture(live=True)
    g = cross.gated_trades(feat, mkts, binance, z0=6.0, health=True)
    assert len(g) == len(cross.GATE_LAGS) * len(cross.GATE_THETAS)  # a running feed trades as before
    feat, mkts, binance = _gated_fixture(live=False)
    assert len(cross.gated_trades(feat, mkts, binance, z0=6.0)) == len(g)
    assert cross.gated_trades(feat, mkts, binance, z0=6.0, health=True).empty  # a frozen book does not
    feat, mkts, binance = _gated_fixture(live=True)
    feat["up_best_bid"] = 0.55  # crossed while the ask is 0.50: never bought there, only once the ask is 0.70
    g = cross.gated_trades(feat, mkts, binance, z0=6.0, health=True)
    assert len(g) and (g["price"] == 0.70).all()
    feat, _, _ = _gated_fixture(live=True)
    feat["observed_halt_flag"] = True
    assert cross.gated_trades(feat, mkts, binance, z0=6.0, health=True).empty


def _live_book(ts, t_jump, before, after):
    """Snapshots whose Up/Down tops switch from `before` to `after` at t_jump; sizes tick every
    100 ms like a running feed. before/after: (up_bid, up_ask, down_bid, down_ask)."""
    b = np.array([before if t < t_jump else after for t in ts])
    return pd.DataFrame({"timestamp_ms": ts, "market_id": "m5", "lifecycle_state": "active",
                         "up_best_bid": b[:, 0], "up_best_ask": b[:, 1], "down_best_bid": b[:, 2],
                         "down_best_ask": b[:, 3], "up_ask_size": 25.0 + np.arange(len(ts)) % 7,
                         "down_ask_size": 30.0, "up_bid_size": 10.0, "down_bid_size": 10.0})


def test_fade_buys_the_side_a_spotless_jump_made_cheap():
    """Up jumps 0.50 -> 0.65 at E-100 s while Binance is flat: fair stays about 0.50, so Down at the
    0.36 ask clears every theta up to 12c at every jump size; Down wins."""
    start, t_jump = E - 300_000, E - 100_000
    ts = np.arange(start, E, 100)
    feat = _live_book(ts, t_jump, (0.49, 0.51, 0.49, 0.51), (0.64, 0.66, 0.34, 0.36))
    mkts = pd.DataFrame({"market_id": ["m5"], "start": [start], "end": [E], "k": [100.0], "up_won": [0.0], "horizon": [5]})
    rng = np.random.default_rng(3)
    tt = np.arange(E - 1_200_000, E, 250)
    price = 80_000 * np.exp(np.cumsum(rng.normal(0, 1e-5, len(tt))))
    price[tt >= t_jump - 60_000] = price[np.searchsorted(tt, t_jump - 60_000)]  # flat around the jump
    binance = pd.DataFrame({"trade_ts_ms": tt, "recv_ts_ms": tt + 150, "price": price})
    f = cross.fade_trades(feat, mkts, binance)
    assert len(f) == len(cross.FADE_JUMPS) * len(cross.FADE_THETAS)
    assert (f["t"] == t_jump).all() and (f["price"] == 0.36).all() and (f["won"] == 1.0).all()
    assert f["fair"].iloc[0] == pytest.approx(0.50, abs=0.01)
    stalled = feat.assign(up_ask_size=25.0)  # the same prices from a frozen feed: nothing
    assert cross.fade_trades(stalled, mkts, binance).empty


def test_follow_copies_large_takers_within_a_cent():
    start, t_print = E - 300_000, E - 100_000
    ts = np.arange(start, E, 100)
    feat = _live_book(ts, t_print + 200, (0.49, 0.50, 0.49, 0.51), (0.50, 0.51, 0.48, 0.50))
    mkts = pd.DataFrame({"market_id": ["m5"], "start": [start], "end": [E], "k": [100.0], "up_won": [1.0],
                         "horizon": [5], "up_token": ["u"], "down_token": ["d"]})
    trades = pd.DataFrame({"recv_ts_ms": [t_print - 5_000, t_print], "instrument": ["u", "u"], "price": [0.50, 0.50],
                           "size": [100.0, 5000.0], "taker_side": ["buy", "buy"]})
    f = cross.follow_trades(feat, mkts, trades)
    assert f["min_usdc"].tolist() == list(cross.FOLLOW_SIZES) and (f["t"] == t_print).all()
    assert (f["side"] == "Up").all() and (f["price"] == 0.51).all()
    assert f["pnl"].iloc[0] == pytest.approx(1 - 0.51 - 0.07 * 0.51 * 0.49)
    sold = trades.assign(taker_side="sell", price=0.48)  # selling Up at 0.48 bets on Down at 0.52: Down ask 0.50
    g = cross.follow_trades(feat, mkts, sold)
    assert (g["side"] == "Down").all() and (g["price"] == 0.50).all() and (g["won"] == 0.0).all()
    far = trades.assign(price=0.45)  # the ask is now 6c above the print: not followed
    assert cross.follow_trades(feat, mkts, far).empty


def test_gated_trades_measure_other_takers():
    """Other takers buy 40 Up at 0.50 between the print's receipt and our order (300 ms), and 30
    more in the 300 ms after it; a 0.60 buy and a Down buy do not count."""
    feat, mkts, binance = _gated_fixture(live=True)
    mkts = mkts.assign(up_token="u", down_token="d")
    t0 = E - 100_000 + 150
    trades = pd.DataFrame({"recv_ts_ms": [t0 + 100, t0 + 200, t0 + 250, t0 + 400], "instrument": ["u", "u", "d", "u"],
                           "price": [0.50, 0.60, 0.40, 0.50], "size": [40.0, 99.0, 50.0, 30.0],
                           "taker_side": ["buy", "buy", "buy", "buy"]})
    g = cross.gated_trades(feat, mkts, binance, z0=6.0, trades=trades)
    r = g[(g["lag"] == 300) & (g["theta"] == 0.12)].iloc[0]
    assert (r["ask0"], r["price"], r["taken_before"], r["taken_next"]) == (0.50, 0.50, 40.0, 30.0)
    assert cross.gated_trades(feat, mkts, binance, z0=6.0)["taken_before"].isna().all()
