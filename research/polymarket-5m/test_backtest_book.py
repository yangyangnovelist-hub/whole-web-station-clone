import numpy as np
import pandas as pd
import pytest

import backtest_book as bb
import binary as bo
import simulate as sim


def fair_market_fixture(tmp_path, n=2000, seed=1, first_close="2026-03-01", layout="kacho"):
    """Synthetic 5m markets whose mid is the true probability (a calibrated market)."""
    rng = np.random.default_rng(seed)
    sig = bo.per_second_vol(0.45 * np.exp(0.35 * rng.standard_normal(n)))
    x, _ = sim.simulate_paths(sig, rng)
    strike = x[:, :sim.PRE].mean(axis=1)
    up = x[:, sim.PRE + sim.T - sim.W:].mean(axis=1) >= strike
    close = pd.Timestamp(first_close, tz="UTC").timestamp() + 300.0 * np.arange(1, n + 1)
    cid = np.array([f"0x{i:06x}" for i in range(n)])

    ticks = []
    for t in range(150, sim.T):
        xt = x[:, sim.PRE - 1 + t]
        ks = x[:, sim.PRE + sim.T - sim.W: sim.PRE + t].sum(axis=1)
        bid, ask = bo.quote_book(bo.prob_up(xt, strike, sig, t, ks))
        ticks.append(pd.DataFrame({"cid": cid, "ts": close - sim.T + t, "bid": bid, "ask": ask}))
    ticks = pd.concat(ticks, ignore_index=True)

    if layout == "kacho":
        markets = pd.DataFrame({"condition_id": cid,
                                "end_ts": pd.to_datetime(close, unit="s", utc=True),
                                "outcome": np.where(up, "Up", "Down")})
        ticks = pd.DataFrame({"condition_id": ticks["cid"],
                              "ts_utc": pd.to_datetime(ticks["ts"], unit="s", utc=True),
                              "bu": ticks["bid"], "au": ticks["ask"]})
    else:  # unfamiliar names, ms epochs, prices in cents, start instead of end, 0/1 outcome
        markets = pd.DataFrame({"mkt": cid, "window_start": (close - 300) * 1000, "up_won": up.astype(int)})
        ticks = pd.DataFrame({"mkt": ticks["cid"], "epoch_ms": (ticks["ts"] * 1000).astype(np.int64),
                              "best_bid": ticks["bid"] * 100, "best_ask": ticks["ask"] * 100})
    mp, tp = tmp_path / "btc_markets.parquet", tmp_path / "btc_ticks.parquet"
    markets.to_parquet(mp)
    ticks.to_parquet(tp)
    return mp, tp, up


@pytest.fixture(scope="module")
def kacho(tmp_path_factory):
    return fair_market_fixture(tmp_path_factory.mktemp("kacho"))


def test_loads_kacho_layout_and_snapshots_every_tau(kacho):
    mp, tp, up = kacho
    markets, ticks = bb.load([mp], [tp], {})
    assert len(markets) == len(up) and markets["up"].sum() == up.sum()
    snap = bb.snapshots(markets, ticks)
    assert snap.groupby("tau").size().to_dict() == {tau: len(up) for tau in bb.TAUS}
    # Snapshot sits exactly tau seconds before the close.
    assert np.allclose(snap["end"] - snap["ts"], snap["tau"])


def test_calibrated_market_has_no_gross_edge_and_loses_costs(kacho):
    mp, tp, _ = kacho
    snap = bb.snapshots(*bb.load([mp], [tp], {}))
    s60 = snap[snap["tau"] == 60]
    slope = np.polyfit(s60["mid"], s60["up"], 1)[0]
    assert slope == pytest.approx(1.0, abs=0.08)

    t = bb.trades(snap[snap["tau"] == 120], "fav", 0.80, 0.97, bo.CRYPTO_FEE_RATE)
    assert len(t["pnl"]) > 300
    assert abs(t["gross"].mean()) < 0.02
    assert t["pnl"].mean() == pytest.approx(t["gross"].mean() - t["spread"].mean() - t["fee"].mean())
    assert -0.03 < t["pnl"].mean() < 0.005


def test_unfamiliar_layout_with_overrides(tmp_path):
    mp, tp, up = fair_market_fixture(tmp_path, n=300, layout="other")
    overrides = bb.parse_overrides(["markets.cid=mkt", "ticks.cid=mkt", "ticks.ts=epoch_ms",
                                    "ticks.bid_up=best_bid", "ticks.ask_up=best_ask"])
    markets, ticks = bb.load([mp], [tp], overrides)
    assert markets["up"].sum() == up.sum()
    assert ticks["bid_up"].max() <= 1.0
    snap = bb.snapshots(markets, ticks)
    assert np.allclose(snap["end"] - snap["ts"], snap["tau"])


def test_missing_column_names_the_fix(tmp_path):
    mp, tp, _ = fair_market_fixture(tmp_path, n=50, layout="other")
    with pytest.raises(SystemExit, match="--col markets.cid"):
        bb.load([mp], [tp], {})


def test_complementary_down_book_improves_prices():
    df = pd.DataFrame({"bid_up": [0.40], "ask_up": [0.45], "bid_down": [0.57], "ask_down": [0.58]})
    bid_up, ask_up, ask_down = bb.effective_book(df)
    assert bid_up[0] == pytest.approx(0.42) and ask_up[0] == pytest.approx(0.43)
    assert ask_down[0] == pytest.approx(0.58)


def test_era_split_and_report(tmp_path):
    mp, tp, _ = fair_market_fixture(tmp_path, n=200, first_close="2026-08-13T23:30:00")
    markets, ticks = bb.load([mp], [tp], {})
    snap = bb.snapshots(markets, ticks)
    assert set(bb.era_of(snap["end"])) == {"30s TWAP", "60s TWAP"}
    text = bb.report(markets, snap, bo.CRYPTO_FEE_RATE)
    assert "## 30s TWAP" in text and "## 60s TWAP" in text and "聚类 t" in text


def test_to_seconds_units():
    ref = pd.Timestamp("2026-05-01", tz="UTC").timestamp()
    for value in (ref, ref * 1e3, ref * 1e6, ref * 1e9):
        assert bb.to_seconds(pd.Series([value]))[0] == pytest.approx(ref)
    assert bb.to_seconds(pd.Series(["2026-05-01T00:00:00Z"]))[0] == pytest.approx(ref)
